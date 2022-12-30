from unittest.mock import patch

from ase import units
from ase.db import connect
from pytest import mark, fixture, raises
from ase.build import molecule
from ase.calculators.lj import LennardJones

from examol.simulate.ase import ASESimulator
from examol.simulate.ase.utils import write_to_string


class FakeCP2K(LennardJones):

    def __init__(self, *args, **kwargs):
        super().__init__()

    def __del__(self):
        return


@fixture()
def strc() -> str:
    atoms = molecule('H2O')
    return write_to_string(atoms, 'xyz')


def test_config_maker(tmpdir):
    sim = ASESimulator(scratch_dir=tmpdir)

    # Easy example
    config = sim.create_configuration('cp2k_blyp_szv', charge=0, solvent=None)
    assert config['kwargs']['cutoff'] == 600 * units.Ry

    # With a charge
    config = sim.create_configuration('cp2k_blyp_szv', charge=1, solvent=None)
    assert config['kwargs']['cutoff'] == 600 * units.Ry
    assert config['kwargs']['charge'] == 1
    assert config['kwargs']['uks']

    # With an undefined basis set
    with raises(AssertionError):
        sim.create_configuration('cp2k_blyp_notreal', charge=1, solvent=None)


@mark.parametrize('config_name', ['cp2k_blyp_szv'])
def test_ase(config_name: str, strc, tmpdir):
    with patch('ase.calculators.cp2k.CP2K', new=FakeCP2K):
        db_path = str(tmpdir / 'data.db')
        sim = ASESimulator(scratch_dir=tmpdir, ase_db_path=db_path)
        out_res, traj_res, extra = sim.optimize_structure(strc, config_name, charge=1)
        assert out_res.energy < traj_res[0].energy

        # Make sure everything is stored in the DB
        with connect(db_path) as db:
            assert len(db) == len(traj_res)
            assert next(db.select())['total_charge'] == 1

        # Make sure it doesn't write new stuff
        sim.optimize_structure(strc, config_name, charge=1)
        assert len(db) == len(traj_res)
        assert next(db.select())['total_charge'] == 1
