"""Train neural network models using `NFP <https://github.com/NREL/nfp>`_"""

from sklearn.model_selection import train_test_split
try:
    from tensorflow.keras import callbacks as cb
except ImportError as e:  # pragma: no-coverage
    raise ImportError('You may need to install Tensorflow and NFP.') from e
import tensorflow as tf
import numpy as np
import nfp

from examol.store.models import MoleculeRecord
from examol.utils.conversions import convert_string_to_nx
from .base import Scorer
from .utils.tf import LRLogger, TimeLimitCallback, EpochTimeLogger


class ReduceAtoms(tf.keras.layers.Layer):
    """Reduce the atoms along a certain direction

    Args:
        reduction_op: Name of the operation used for reduction
    """

    def __init__(self, reduction_op: str = 'mean', **kwargs):
        super().__init__(**kwargs)
        self.reduction_op = reduction_op

    def get_config(self):
        config = super().get_config()
        config['reduction_op'] = self.reduction_op
        return config

    def call(self, inputs, mask=None):  # pragma: no-coverage
        """
        Args:
            inputs: Matrix to be reduced
            mask: Identifies which rows to sum are placeholders
        """
        masked_tensor = tf.ragged.boolean_mask(inputs, mask)
        reduce_fn = getattr(tf.math, f'reduce_{self.reduction_op}')
        return reduce_fn(masked_tensor, axis=1)


# Define the custom layers for our class
custom_objects = nfp.custom_objects.copy()
custom_objects['ReduceAtoms'] = ReduceAtoms


def make_simple_network(
        atom_features: int = 64,
        message_steps: int = 8,
        output_layers: list[int] = (512, 256, 128),
        reduce_op: str = 'mean',
        atomwise: bool = True,
) -> tf.keras.models.Model:
    """Construct a Keras model using the settings provided by a user

    Args:
        atom_features: Number of features used per atom and bond
        message_steps: Number of message passing steps
        output_layers: Number of neurons in the readout layers
        reduce_op: Operation used to reduce from atom-level to molecule-level vectors
        atomwise: Whether to reduce atomwise contributions to form an output,
                  or reduce to a single vector per molecule before the output layers
    Returns:
        A model instantiated with the user-defined options
    """
    atom = tf.keras.layers.Input(shape=[None], dtype=tf.int32, name='atom')
    bond = tf.keras.layers.Input(shape=[None], dtype=tf.int32, name='bond')
    connectivity = tf.keras.layers.Input(shape=[None, 2], dtype=tf.int32, name='connectivity')

    # Convert from a single integer defining the atom state to a vector
    # of weights associated with that class
    atom_state = tf.keras.layers.Embedding(64, atom_features, name='atom_embedding', mask_zero=True)(atom)

    # Ditto with the bond state
    bond_state = tf.keras.layers.Embedding(5, atom_features, name='bond_embedding', mask_zero=True)(bond)

    # Here we use our first nfp layer. This is an attention layer that looks at
    # the atom and bond states and reduces them to a single, graph-level vector.
    # mum_heads * units has to be the same dimension as the atom / bond dimension
    global_state = nfp.GlobalUpdate(units=4, num_heads=1, name='problem')([atom_state, bond_state, connectivity])

    for _ in range(message_steps):  # Do the message passing
        new_bond_state = nfp.EdgeUpdate()([atom_state, bond_state, connectivity, global_state])
        bond_state = tf.keras.layers.Add()([bond_state, new_bond_state])

        new_atom_state = nfp.NodeUpdate()([atom_state, bond_state, connectivity, global_state])
        atom_state = tf.keras.layers.Add()([atom_state, new_atom_state])

        new_global_state = nfp.GlobalUpdate(units=4, num_heads=1)(
            [atom_state, bond_state, connectivity, global_state]
        )
        global_state = tf.keras.layers.Add()([global_state, new_global_state])

    # Pass the global state through an output
    output = atom_state
    if not atomwise:
        output = ReduceAtoms(reduce_op)(output)
    for shape in output_layers:
        output = tf.keras.layers.Dense(shape, activation='relu')(output)
    output = tf.keras.layers.Dense(1)(output)
    if atomwise:
        output = ReduceAtoms(reduce_op)(output)
    output = tf.keras.layers.Dense(1, activation='linear', name='scale')(output)

    # Construct the tf.keras model
    return tf.keras.Model([atom, bond, connectivity], [output])


class NFPMessage:
    """Package for sending an MPNN model over connections that require pickling"""

    def __init__(self, model: tf.keras.Model):
        """
        Args:
            model: Model to be sent
        """

        self.config = model.to_json()
        # Makes a copy of the weights to ensure they are not memoryview objects
        self.weights = [np.array(v) for v in model.get_weights()]

        # Cached copy of the model
        self._model = model

    def __getstate__(self):
        """Get state except the model"""
        state = self.__dict__.copy()
        state['_model'] = None
        return state

    def get_model(self) -> tf.keras.Model:
        """Get a copy of the model

        Returns:
            The model specified by this message
        """
        if self._model is None:
            self._model = tf.keras.models.model_from_json(
                self.config,
                custom_objects=custom_objects
            )
            self._model.set_weights(self.weights)
        return self._model


def convert_string_to_dict(mol_string: str) -> dict:
    """Convert a molecule to an NFP-compatible dictionary form

    Args:
        mol_string: SMILES or InChI string
    Returns:
        Dictionary
    """

    # Convert first to a nx.Graph
    graph = convert_string_to_nx(mol_string)

    # Get the atom types
    atom_type_id = [n['atomic_num'] for _, n in graph.nodes(data=True)]

    # Get the bond types, making the data
    bond_types = ["", "AROMATIC", "DOUBLE", "SINGLE", "TRIPLE"]  # 0 is a dummy type
    connectivity = []
    edge_type = []
    for a, b, d in graph.edges(data=True):
        connectivity.append([a, b])
        connectivity.append([b, a])
        edge_type.append(str(d['bond_type']))
        edge_type.append(str(d['bond_type']))
    edge_type_id = list(map(bond_types.index, edge_type))

    # Sort connectivity array by the first column
    #  This is needed for the MPNN code to efficiently group messages for
    #  each node when performing the message passing step
    connectivity = np.array(connectivity)
    if connectivity.size > 0:
        # Skip a special case of a molecule w/o bonds
        inds = np.lexsort((connectivity[:, 1], connectivity[:, 0]))
        connectivity = connectivity[inds, :]

        # Tensorflow's "segment_sum" will cause problems if the last atom
        #  is not bonded because it returns an array
        if connectivity.max() != len(atom_type_id) - 1:
            raise ValueError(f"Problem with unconnected atoms for \"{mol_string}\"")
    else:
        connectivity = np.zeros((0, 2))

    return {
        'atom': atom_type_id,
        'bond': edge_type_id,
        'connectivity': connectivity
    }


def make_data_loader(mol_dicts: list[dict],
                     values: np.ndarray | list[object] | None = None,
                     batch_size: int = 32,
                     repeat: bool = False,
                     shuffle_buffer: int | None = None,
                     value_spec: tf.TensorSpec = tf.TensorSpec((), dtype=tf.float32),
                     drop_last_batch: bool = False) -> tf.data.Dataset:
    """Make an in-memory data loader for data compatible with NFP-style neural networks

    Args:
        mol_dicts: List of molecules parsed into the moldesign format
        values: List of output values, if included in the output
        value_spec: Tensorflow specification for the output
        batch_size: Number of molecules per batch
        repeat: Whether to create an infinitely-repeating iterator
        shuffle_buffer: Size of a shuffle buffer. Use ``None`` to leave data unshuffled
        drop_last_batch: Whether to keep the last batch in the dataset. Set to ``True`` if, for example, you need every batch to be the same size
    Returns:
        Data loader that generates molecules in the desired shapes
    """

    # Determine the maximum size of molecule, used when padding the arrays
    max_atoms = max(len(x['atom']) for x in mol_dicts)
    max_bonds = max(len(x['bond']) for x in mol_dicts)

    # Make the initial data loader
    record_sig = {
        "atom": tf.TensorSpec(shape=(None,), dtype=tf.int32),
        "bond": tf.TensorSpec(shape=(None,), dtype=tf.int32),
        "connectivity": tf.TensorSpec(shape=(None, 2), dtype=tf.int32),
    }
    if values is None:
        def generator():
            yield from mol_dicts
    else:
        def generator():
            yield from zip(mol_dicts, values)

        record_sig = (record_sig, value_spec)

    loader = tf.data.Dataset.from_generator(generator=generator, output_signature=record_sig).cache()  # TODO (wardlt): Make caching optional?

    # Repeat the molecule list before shuffling
    if repeat:
        loader = loader.repeat()

    # Shuffle, if desired
    if shuffle_buffer is not None:
        loader = loader.shuffle(shuffle_buffer)

    # Make the batches. Pads the data to make them all the same size, adding 0's to signify padded values
    padded_records = {
        "atom": tf.TensorShape((max_atoms,)),
        "bond": tf.TensorShape((max_bonds,)),
        "connectivity": tf.TensorShape((max_bonds, 2))
    }
    if values is not None:
        padded_records = (padded_records, value_spec.shape)
    loader = loader.padded_batch(batch_size=batch_size, padded_shapes=padded_records, drop_remainder=drop_last_batch)

    return loader


class NFPScorer(Scorer):
    """Train message-passing neural networks based on the `NFP <https://github.com/NREL/nfp>`_ library.

    NFP uses Keras to define message-passing networks, which is backed by Tensorflow for executing the networks on different hardware."""

    def __init__(self, retrain_from_scratch: bool = True):
        """
        Args:
            retrain_from_scratch: Whether to retrain models from scratch or not
        """
        self.retrain_from_scratch = retrain_from_scratch

    def prepare_message(self, model: tf.keras.models.Model, training: bool = False) -> dict | NFPMessage:
        if training and self.retrain_from_scratch:
            return model.get_config()
        else:
            return NFPMessage(model)

    def transform_inputs(self, record_batch: list[MoleculeRecord]) -> list:
        return [convert_string_to_dict(record.identifier.inchi) for record in record_batch]

    def score(self, model_msg: NFPMessage, inputs: list[dict], batch_size: int = 64, **kwargs) -> np.ndarray:
        """Assign a score to molecules

        Args:
            model_msg: Model in a transmittable format
            inputs: Batch of inputs ready for the model (in dictionary format)
            batch_size: Number of molecules to evaluate at each time
        Returns:
            The scores to a set of records
        """
        model = model_msg.get_model()  # Unpack the model
        loader = make_data_loader(inputs, batch_size=batch_size)
        return model.predict(loader, verbose=False)

    def retrain(self,
                model_msg: dict | NFPMessage,
                inputs: list,
                outputs: np.ndarray,
                num_epochs: int = 4,
                batch_size: int = 32,
                validation_split: float = 0.1,
                learning_rate: float = 1e-3,
                device_type: str = 'gpu',
                steps_per_exec: int = 1,
                patience: int = None,
                timeout: float = None,
                verbose: bool = False) -> tuple[list[np.ndarray], dict]:
        """Retrain the scorer based on new training records

        Args:
            model_msg: Model to be retrained
            inputs: Training set inputs, as generated by :meth:`transform_inputs`
            outputs: Training Set outputs, as generated by :meth:`transform_outputs`
            num_epochs: Maximum number of epochs to run
            batch_size: Number of molecules per training batch
            validation_split: Fraction of molecules used for the training/validation split
            learning_rate: Learning rate for the Adam optimizer
            device_type: Type of device used for training
            steps_per_exec: Number of training steps to run per execution on acceleration
            patience: Number of epochs without improvement before terminating training. Default is 10% of ``num_epochs``
            timeout: Maximum training time in seconds
            verbose: Whether to print training information to screen
        Returns:
            Message defining how to update the model
        """

        # Make the model
        if isinstance(model_msg, NFPMessage):
            model = model_msg.get_model()
        elif isinstance(model_msg, dict):
            model = tf.keras.Model.from_config(model_msg, custom_objects=custom_objects)
        else:
            raise NotImplementedError(f'Unrecognized message type: {type(model_msg)}')

        # Split off a validation set
        train_x, valid_x, train_y, valid_y = train_test_split(inputs, outputs, test_size=validation_split)

        # Make the loaders
        steps_per_epoch = len(train_x) // batch_size
        train_loader = make_data_loader(train_x, train_y, repeat=True, batch_size=batch_size, drop_last_batch=True, shuffle_buffer=32768)
        valid_steps = len(valid_x) // batch_size
        assert valid_steps > 0, 'We need some validation data'
        valid_loader = make_data_loader(valid_x, valid_y, batch_size=batch_size, drop_last_batch=True)

        # Define initial guesses for the "scaling" later
        try:
            scale_layer = model.get_layer('scale')
            outputs = np.array(outputs)
            scale_layer.set_weights([outputs.std()[None, None], outputs.mean()[None]])
        except ValueError:
            pass

        # Configure the LR schedule
        init_learn_rate = learning_rate
        final_learn_rate = init_learn_rate * 1e-3
        decay_rate = (final_learn_rate / init_learn_rate) ** (1. / (num_epochs - 1))

        def lr_schedule(epoch, lr):
            return lr * decay_rate

        # Compile the model then train
        model.compile(
            tf.optimizers.Adam(init_learn_rate),
            'mean_squared_error',
            metrics=['mean_absolute_error'],
            steps_per_execution=steps_per_exec,
        )

        # Make the callbacks
        if patience is None:
            patience = num_epochs // 10
        early_stopping = cb.EarlyStopping(patience=patience, restore_best_weights=True)
        callbacks = [
            LRLogger(),
            EpochTimeLogger(),
            cb.LearningRateScheduler(lr_schedule),
            early_stopping,
            cb.TerminateOnNaN(),
        ]
        if timeout is not None:
            callbacks.append(TimeLimitCallback(timeout))
        if timeout is not None:
            callbacks.append(TimeLimitCallback(timeout))

        history = model.fit(
            train_loader,
            epochs=num_epochs,
            shuffle=False,
            verbose=verbose,
            callbacks=callbacks,
            steps_per_epoch=steps_per_epoch,
            validation_data=valid_loader,
            validation_steps=valid_steps,
            validation_freq=1,
        )

        # If a timeout is used, make sure we are using the best weights
        #  The training may have exited without storing the best weights
        if timeout is not None:
            model.set_weights(early_stopping.best_weights)

        # Convert weights to numpy arrays (avoids mmap issues)
        weights = []
        for v in model.get_weights():
            v = np.array(v)
            if np.isnan(v).any():
                raise ValueError('Found some NaN weights.')
            weights.append(v)

        # Once we are finished training call "clear_session" to flush the model out of GPU memory
        tf.keras.backend.clear_session()
        return weights, history.history

    def update(self, model: tf.keras.models.Model, update_msg: tuple[list[np.ndarray], dict]) -> tf.keras.models.Model:
        model.set_weights(update_msg[0])
        return model
