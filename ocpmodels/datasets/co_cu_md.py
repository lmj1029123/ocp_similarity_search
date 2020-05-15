import os

import ase
import numpy as np
import torch
from ase.io.trajectory import Trajectory
from pymatgen.io.ase import AseAtomsAdaptor
from torch_geometric.data import Data, DataLoader
from tqdm import tqdm

from ocpmodels.common.registry import registry
from ocpmodels.common.utils import collate
from ocpmodels.datasets import BaseDataset
from ocpmodels.datasets.elemental_embeddings import EMBEDDINGS
from ocpmodels.datasets.gasdb import AtomicFeatureGenerator, GaussianDistance


@registry.register_dataset("co_cu_md")
class COCuMD(BaseDataset):
    def __init__(self, config, transform=None, pre_transform=None):
        super(BaseDataset, self).__init__(config, transform, pre_transform)

        self.config = config

        try:
            self.data, self.slices = torch.load(self.processed_file_names[0])
            print(
                "### Loaded preprocessed data from:  {}".format(
                    self.processed_file_names
                )
            )
        except FileNotFoundError:
            self.process()

    @property
    def raw_file_names(self):
        return [os.path.join(self.config["src"], self.config["traj"])]

    @property
    def processed_file_names(self):
        return [
            os.path.join(
                self.config["src"], "processed", self.config["traj"] + ".pt"
            )
        ]

    def process(self):
        print(
            "### Preprocessing atoms objects from:  {}".format(
                self.raw_file_names[0]
            )
        )
        traj = Trajectory(self.raw_file_names[0])
        feature_generator = TrajectoryFeatureGenerator(traj)

        positions = [i.get_positions() for i in traj]
        forces = [i.get_forces(apply_constraint=False) for i in traj]
        p_energies = [
            i.get_potential_energy(apply_constraint=False) for i in traj
        ]

        data_list = []
        zipped_data = zip(feature_generator, positions, forces, p_energies)

        for (embedding, distance, index), pos, force, p_energy in tqdm(
            zipped_data,
            desc="preprocessing atomic features",
            total=len(p_energies),
            unit="structure",
        ):

            edge_index = [[], []]
            edge_attr = torch.FloatTensor(
                index.shape[0] * index.shape[1], distance.shape[-1]
            )
            for j in range(index.shape[0]):
                for k in range(index.shape[1]):
                    edge_index[0].append(j)
                    edge_index[1].append(index[j, k])
                    edge_attr[j * index.shape[1] + k] = distance[j, k].clone()
            edge_index = torch.LongTensor(edge_index)
            data_list.append(
                Data(
                    x=embedding,
                    edge_index=edge_index,
                    edge_attr=edge_attr,
                    y=p_energy,
                    pos=torch.tensor(pos),
                    force=torch.tensor(force),
                )
            )

        self.data, self.slices = collate(data_list)
        torch.save((self.data, self.slices), self.processed_file_names[0])

    def get_dataloaders(self, batch_size=None):
        assert batch_size is not None
        assert self.train_size + self.val_size + self.test_size <= len(self)

        test_dataset = self[
            self.train_size
            + self.val_size : self.train_size
            + self.val_size
            + self.test_size
        ]
        train_val_dataset = self[: self.train_size + self.val_size].shuffle()

        train_loader = DataLoader(
            train_val_dataset[: self.train_size],
            batch_size=batch_size,
            shuffle=True,
        )
        val_loader = DataLoader(
            train_val_dataset[
                self.train_size : self.train_size + self.val_size
            ],
            batch_size=batch_size,
        )
        test_loader = DataLoader(test_dataset, batch_size=batch_size)

        return train_loader, val_loader, test_loader


class TrajectoryFeatureGenerator(AtomicFeatureGenerator):
    """
    Iterator meant to generate the features of the atoms objects within a trajectory

    Parameters
    ----------

    traj: instance of `ase.io.trajectory.Trajectory`
        MD trajactory of `ase.Atoms` objects
    max_num_nbr: int
        The maximum number of neighbors while constructing the crystal graph
    radius: float
        The cutoff radius for searching neighbors
    dmin: float
        The minimum distance for constructing GaussianDistance
    step: float
        The step size for constructing GaussianDistance

    Returns
    -------

    embeddings: torch.Tensor shape (n_i, atom_fea_len)
    gaussian_distances: torch.Tensor shape (n_i, M, nbr_fea_len)
    all_indices: torch.LongTensor shape (n_i, M)
    """

    def __init__(
        self, traj, max_num_nbr=12, radius=6, dmin=0, step=0.2, start=0
    ):
        self.traj = traj
        super(TrajectoryFeatureGenerator, self).__init__(
            None, max_num_nbr, radius, dmin, step, start
        )

    def __len__(self):
        return len(self.traj)

    def __next__(self):
        try:
            item = self.__getitem__(self.num)
            self.num += 1
            return item

        except IndexError:
            assert self.num == self.__len__()
            raise StopIteration

    def __getitem__(self, index):
        atoms = self.traj[index]
        return self.extract_atom_features(atoms)