#
# Copyright (C) 2014 Jerome Kelleher <jerome.kelleher@well.ox.ac.uk>
#
# This file is part of msprime.
#
# msprime is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# msprime is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with msprime.  If not, see <http://www.gnu.org/licenses/>.
#
"""
Module responsible to generating and reading tree files.
"""
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import collections
import heapq
import json
import os
import platform
import random
import sys
import tempfile

import h5py
import h5py.h5
import numpy as np

import _msprime
from _msprime import InputError
from _msprime import LibraryError
from . import __version__


def harmonic_number(n):
    """
    Returns the nth Harmonic number.
    """
    return sum(1 / k for k in range(1, n + 1))

def simulate_trees(sample_size, num_loci, scaled_recombination_rate,
        population_models=[], random_seed=None, max_memory="10M"):
    """
    Simulates the coalescent with recombination under the specified model
    parameters and returns an iterator over the resulting trees.
    """
    sim = TreeSimulator(sample_size)
    sim.set_num_loci(num_loci)
    sim.set_scaled_recombination_rate(scaled_recombination_rate)
    if random_seed is not None:
        sim.set_random_seed(random_seed)
    sim.set_max_memory(max_memory)
    for m in population_models:
        sim.add_population_model(m)
    tree_sequence = sim.run()
    return tree_sequence.sparse_trees()

def simulate_tree(sample_size, population_models=[], random_seed=None,
        max_memory="10M"):
    """
    Simulates the coalescent at a single locus for the specified sample size
    under the specified list of population models.
    """
    iterator = simulate_trees(sample_size, 1, 0, population_models,
            random_seed, max_memory)
    l, pi, tau = next(iterator)
    return pi, tau

class TreeSimulator(object):
    """
    Class to simulate trees under the standard neutral coalescent with
    recombination.
    """
    def __init__(self, sample_size):
        self._sample_size = sample_size
        self._scaled_recombination_rate = 1.0
        self._num_loci = 1
        self._population_models = []
        self._random_seed = None
        self._segment_block_size = None
        self._avl_node_block_size = None
        self._node_mapping_block_size = None
        self._coalescence_record_block_size = None
        self._max_memory = None
        self._ll_sim = None

    def get_sample_size(self):
        return self._sample_size

    def get_scaled_recombination_rate(self):
        return self._scaled_recombination_rate

    def get_num_loci(self):
        return self._num_loci

    def get_random_seed(self):
        return self._random_seed

    def get_population_models(self):
        return self._population_models

    def get_num_breakpoints(self):
        return self._ll_sim.get_num_breakpoints()

    def get_used_memory(self):
        return self._ll_sim.get_used_memory()

    def get_time(self):
        return self._ll_sim.get_time()

    def get_num_avl_node_blocks(self):
        return self._ll_sim.get_num_avl_node_blocks()

    def get_num_coalescence_record_blocks(self):
        return self._ll_sim.get_num_coalescence_record_blocks()

    def get_num_node_mapping_blocks(self):
        return self._ll_sim.get_num_node_mapping_blocks()

    def get_num_segment_blocks(self):
        return self._ll_sim.get_num_segment_blocks()

    def get_num_coancestry_events(self):
        return self._ll_sim.get_num_coancestry_events()

    def get_num_recombination_events(self):
        return self._ll_sim.get_num_recombination_events()


    def get_max_memory(self):
        return self._ll_sim.get_max_memory()

    def add_population_model(self, pop_model):
        self._population_models.append(pop_model)

    def set_num_loci(self, num_loci):
        self._num_loci = num_loci

    def set_scaled_recombination_rate(self, scaled_recombination_rate):
        self._scaled_recombination_rate = scaled_recombination_rate

    def set_effective_population_size(self, effective_population_size):
        self._effective_population_size = effective_population_size

    def set_random_seed(self, random_seed):
        self._random_seed = random_seed

    def set_segment_block_size(self, segment_block_size):
        self._segment_block_size = segment_block_size

    def set_avl_node_block_size(self, avl_node_block_size):
        self._avl_node_block_size = avl_node_block_size

    def set_node_mapping_block_size(self, node_mapping_block_size):
        self._node_mapping_block_size = node_mapping_block_size

    def set_coalescence_record_block_size(self, coalescence_record_block_size):
        self._coalescence_record_block_size = coalescence_record_block_size

    def set_max_memory(self, max_memory):
        """
        Sets the approximate maximum memory used by the simulation
        to the specified value.  This can be suffixed with
        K, M or G to specify units of Kibibytes, Mibibytes or Gibibytes.
        """
        s = max_memory
        d = {"K":2**10, "M":2**20, "G":2**30}
        multiplier = 1
        value = s
        if s.endswith(tuple(d.keys())):
            value = s[:-1]
            multiplier = d[s[-1]]
        n = int(value)
        self._max_memory = n * multiplier

    def _set_environment_defaults(self):
        """
        Sets sensible default values for the memory usage parameters.
        """
        # Set the block sizes using our estimates.
        n = self._sample_size
        m = self._num_loci
        # First check to make sure they are sane.
        if not isinstance(n, int):
            raise TypeError("Sample size must be an integer")
        if not isinstance(m, int):
            raise TypeError("Number of loci must be an integer")
        if n < 2:
            raise ValueError("Sample size must be >= 2")
        if m < 1:
            raise ValueError("Postive number of loci required")
        rho = 4 * self._scaled_recombination_rate * (m - 1)
        num_trees = min(m // 2, rho * harmonic_number(n - 1))
        b = 10 # Baseline maximum
        num_trees = max(b, int(num_trees))
        num_avl_nodes = max(b, 4 * n + num_trees)
        # TODO This is probably much too large now.
        num_segments = max(b, int(0.0125 * n  * rho))
        if self._avl_node_block_size is None:
            self._avl_node_block_size = num_avl_nodes
        if self._segment_block_size is None:
            self._segment_block_size = num_segments
        if self._node_mapping_block_size is None:
            self._node_mapping_block_size = num_trees
        if self._coalescence_record_block_size is None:
            memory = 16 * 2**10  # 16M
            # Each coalescence record is 32bytes
            self._coalescence_record_block_size = memory // 32
        if self._random_seed is None:
            self._random_seed = random.randint(0, 2**31 - 1)
        if self._max_memory is None:
            self._max_memory = 10 * 1024 * 1024 # 10MiB by default

    def run(self):
        """
        Runs the simulation until complete coalescence has occured.
        """
        # Sort the models by start time
        models = sorted(self._population_models, key=lambda m: m.start_time)
        models = [m.get_ll_model() for m in models]
        assert self._ll_sim is None
        self._set_environment_defaults()
        self._ll_sim = _msprime.Simulator(
            sample_size=self._sample_size,
            num_loci=self._num_loci,
            population_models=models,
            scaled_recombination_rate=self._scaled_recombination_rate,
            random_seed=self._random_seed,
            max_memory=self._max_memory,
            segment_block_size=self._segment_block_size,
            avl_node_block_size=self._avl_node_block_size,
            node_mapping_block_size=self._node_mapping_block_size,
            coalescence_record_block_size=self._coalescence_record_block_size)
        self._ll_sim.run()
        records = self._ll_sim.get_coalescence_records()
        left, right, children, parent, time = records
        ts = TreeSequence(
            self._ll_sim.get_breakpoints(),
            left, right, children, parent, time,
            self._get_parameters(), self._get_environment(), True)
        return ts

    def _get_parameters(self):
        """
        Returns a dictionary of all the parameters required to run precisely
        this simulation again in the future. That is, only the parameters
        required to deterministically recreate the same result are included.
        """
        models = [m.get_ll_model() for m in self._population_models]
        parameters = {
            "sample_size": self._sample_size,
            "num_loci": self._num_loci,
            "scaled_recombination_rate": self._scaled_recombination_rate,
            "population_models": models,
            "random_seed": self._random_seed
        }
        return parameters

    def _get_environment(self):
        """
        Returns a dictionary of information on the simulation environment
        that is relevant for reproducing the results of a given simulation.
        """
        return {
            "python_version": platform.python_version(),
            "python_implementation": platform.python_implementation(),
            "byteorder": sys.byteorder,
            "platform": sys.platform,
            "machine": platform.machine(),
            "processor": platform.processor(),
            "word_size": "64" if sys.maxsize > 2**32 else "32",
            "numpy_version": np.__version__,
            "h5py_version": h5py.__version__,
            "hdf5_version": "{}.{}.{}".format(*h5py.h5.get_libversion()),
            "gsl_version": _msprime.get_gsl_version(),
        }

    def reset(self):
        """
        Resets the simulation so that we can perform another replicate.
        """
        self._ll_sim = None

class TreeSequence(object):

    def __init__(self, breakpoints, left, right, children, parent, time,
            parameters, environment, sort_records=False):
        uint32 = "uint32"
        # TODO there is a quite a lot of copying going on here. Do
        # we need to do this?
        self._breakpoints = np.array(breakpoints, dtype=uint32)
        self._num_records = len(left)
        self._left  = np.array(left , dtype=uint32)
        self._right = np.array(right, dtype=uint32)
        self._children = np.array(children, dtype=uint32)
        self._parent = np.array(parent, dtype=uint32)
        self._time = np.array(time, dtype="double")
        self._sample_size = parameters["sample_size"]
        self._num_loci = parameters["num_loci"]
        self._parameters = dict(parameters)
        self._environment = dict(environment)
        if sort_records:
            p = np.argsort(self._left)
            self._left = self._left[p]
            self._right = self._right[p]
            self._children = self._children[p]
            self._parent = self._parent[p]
            self._time = self._time[p]

    def print_state(self):
        print("parameters = ")
        print(json.dumps(self._parameters, sort_keys=True, indent=4))
        print("environment = ")
        print(json.dumps(self._environment, sort_keys=True, indent=4))
        for j in range(self._num_records):
            print(self._left[j], self._right[j], self._children[j],
                    self._parent[j], self._time[j], sep="\t")

    def dump(self, path):
        """
        Writes the tree sequence to the specified file path.
        """
        compression = None
        with h5py.File(path, "w") as f:
            f.attrs["file_version"] = "0.1"
            f.attrs["library_version"] = __version__
            f.attrs["environment"] = json.dumps(self._environment)
            f.attrs["parameters"] = json.dumps(self._parameters)
            uint32 = "uint32"
            f.create_dataset(
                "breakpoints", data=self._breakpoints, dtype=uint32,
                compression=compression)
            records = f.create_group("records")
            records.create_dataset(
                "left", data=self._left, dtype=uint32, compression=compression)
            records.create_dataset(
                "right", data=self._right, dtype=uint32, compression=compression)
            records.create_dataset(
                "children", data=self._children, dtype=uint32, compression=compression)
            records.create_dataset(
                "parent", data=self._parent, dtype=uint32, compression=compression)
            records.create_dataset(
                "time", data=self._time, dtype="double", compression=compression)


    @classmethod
    def load(cls, path):
        with h5py.File(path, "r") as f:
            records = f["records"]
            parameters = json.loads(f.attrs["parameters"])
            environment = json.loads(f.attrs["environment"])
            ret = TreeSequence(
                f["breakpoints"], records["left"], records["right"],
                records["children"], records["parent"], records["time"],
                parameters, environment)
        return ret

    def get_sample_size(self):
        return self._sample_size

    def get_num_loci(self):
        return self._num_loci

    def get_parameters(self):
        return self._parameters

    def get_environment(self):
        return self._environment

    def get_breakpoints(self):
        return self._breakpoints

    def records(self):
        return zip(
            self._left, self._right, self._children, self._parent, self._time)

    def sparse_trees(self):
        n = self._sample_size
        pi = {}
        tau = {j:0 for j in range(1, n + 1)}
        l = 0
        last_l = 0
        live_segments = []
        # print("START")
        for l, r, children, parent, t in self.records():
            if last_l != l:
                # print("YIELDING TREE", len(live_segments))
                # for right, v in live_segments:
                    # print("\t", right, v)
                # q = 1
                # while q in pi:
                #     q = pi[q]
                # pi[q] = 0
                yield l - last_l, pi, tau
                # del pi[q]
                last_l = l
            heapq.heappush(live_segments, (r, (tuple(children), parent)))
            while live_segments[0][0] <= l:
                x, (other_children, p) = heapq.heappop(live_segments)
                # print("Popping off segment", x, children, p)
                for c in other_children:
                    del pi[c]
                del tau[p]
            pi[children[0]] = parent
            pi[children[1]] = parent
            tau[parent] = t
        # q = 1
        # while q in pi:
        #     q = pi[q]
        # pi[q] = 0
        yield self.get_num_loci() - l, pi, tau

    def _diffs(self):
        n = self._sample_size
        left = 0
        used_records = collections.defaultdict(list)
        records_in = []
        for l, r, c, parent, t in self.records():
            # It's a bit wasteful here creating a new tuple, but
            # we get weird results from comparisons downstream if
            # we pass back numpy arrays.
            children = tuple(c)
            if l != left:
                yield l - left, used_records[left], records_in
                del used_records[left]
                records_in = []
                left = l
            used_records[r].append((children, parent, t))
            records_in.append((children, parent, t))
        yield r - left, used_records[left], records_in

    def _diffs_with_breaks(self):
        k = 1
        x = 0
        b = self._breakpoints
        for length, records_out, records_in in self._diffs():
            x += length
            yield b[k] - b[k - 1], records_out, records_in
            while self._breakpoints[k] != x:
                k += 1
                yield b[k] - b[k - 1], [], []
            k += 1

    def diffs(self, all_breaks=False):
        if all_breaks:
            return self._diffs_with_breaks()
        else:
            return self._diffs()

    def newick_trees(self, precision=3, all_breaks=False):
        return NewickGenerator(self, precision, all_breaks)


class NewickGenerator(object):
    """
    An iterator over the newick trees for a given tree sequence.
    """
    def __init__(self, tree_sequence, precision, all_breaks):
        self._tree_sequence = tree_sequence
        self._diffs = tree_sequence.diffs(all_breaks)
        self._sample_size = tree_sequence.get_sample_size()
        self._precision = precision
        self._children = {}
        self._branch_length = {}
        self._time = {j: 0.0 for j in range(1, self._sample_size + 1)}
        self._parent = {}
        self._subtree = {}

    def __iter__(self):
        return self

    def __next__(self):
        return self.next()

    def next(self):
        length, records_out, records_in = next(self._diffs)
        for children, parent, time in records_out:
            del self._children[parent]
            del self._time[parent]
            for j in children:
                self._propogate_subtree_loss(j)
                del self._parent[j]
                del self._branch_length[j]
        for children, parent, time in records_in:
            self._children[parent] = children
            for j in children:
                self._parent[j] = parent
                self._propogate_subtree_loss(j)
            self._time[parent] = time
        # Update the branch_lengths
        for children, parent, time in records_in:
            for j in children:
                s = "{0:.{1}f}".format(
                    time - self._time[j], self._precision)
                self._branch_length[j] = np.char.array(s.encode(), 1)
        self._root = 1
        while self._root in self._parent:
            self._root = self._parent[self._root]
        assert len(self._time) == 2 * self._sample_size - 1
        assert len(self._parent) == 2 * self._sample_size - 2
        assert len(self._branch_length) == 2 * self._sample_size - 2
        self._update_subtrees()
        ret = self._subtree[self._root].tostring()
        return length, ret

    def _propogate_subtree_loss(self, node):
        """
        Propogates the loss of the subtree at the specified node up
        through the tree.
        """
        k = node
        while k in self._subtree:
            del self._subtree[k]
            if k in self._parent:
                k = self._parent[k]

    def _update_subtrees(self):
        # We use a two stack iterative method to do a postorder traversal.
        stack = [self._root]
        nodes = np.zeros(2 * self._sample_size, dtype="uint32")
        k = 0
        while len(stack) != 0:
            node = stack.pop()
            if node not in self._subtree:
                nodes[k] = node
                k += 1
                if node in self._children:
                    for j in self._children[node]:
                        stack.append(j)
        # Now, nodes contains the list of nodes we must visit in
        # reverse order.
        dtype = (bytes, 1)
        nodes = nodes[:k]
        for node in nodes[::-1]:
            if node in self._children:
                children = self._children[node]
                s1 = self._subtree[children[0]]
                s2 = self._subtree[children[1]]
                length = len(s1) + len(s2) + 4
                sep = b";"
                if node != self._root:
                    length += len(self._branch_length[node])
                    sep = b":"
                s = np.empty(length, dtype=dtype)
                s[0] = b"("
                k = 1
                s[k:len(s1) + 1] = s1
                k += len(s1)
                s[k] = b","
                k += 1
                s[k: k + len(s2)] = s2
                k += len(s2)
                s[k] = b")"
                k += 1
                s[k] = sep
                k += 1
                if node != self._root:
                    s[k:] = self._branch_length[node]
            else:
                prefix = np.char.array(str(node).encode() + b":", 1)
                suffix = self._branch_length[node]
                s = np.empty(len(prefix) + len(suffix), dtype=dtype)
                s[:len(prefix)] = prefix
                s[len(prefix):] = suffix
            self._subtree[node] = s


class HaplotypeGenerator(object):
    """
    Class that takes a TreeSequence and a recombination rate and builds a set
    of haplotypes consistent with the underlying trees.
    """
    def __init__(self, tree_sequence, scaled_mutation_rate, random_seed=None):
        self._random_seed = random_seed
        if random_seed is None:
            self._random_seed = random.randint(0, 2**31)
        self._rng = np.random.RandomState(random_seed)
        self._tree_sequence = tree_sequence
        self._num_loci = tree_sequence.get_num_loci()
        self._sample_size = tree_sequence.get_sample_size()
        self._scaled_mutation_rate = scaled_mutation_rate
        self._num_segregating_sites = 0
        self._max_segregating_sites = self._num_loci
        self._total_branch_length = 0
        self._branch_length = {}
        self._children = {}
        self._time = {j: 0 for j in range(1, self._sample_size + 1)}
        self._haplotype = np.empty(
            (self._sample_size, self._max_segregating_sites),
            dtype=(bytes, 1))
        self._haplotype.fill(b"0")
        self._generate_haplotypes()

    def _generate_haplotypes(self):
        branches = np.zeros(2 * self._sample_size - 2, dtype="uint32")
        probabilities = np.zeros(2 * self._sample_size - 2)
        for length, records_out, records_in in self._tree_sequence.diffs():
            for children, parent, time in records_out:
                del self._children[parent]
                del self._time[parent]
                for j in children:
                    self._total_branch_length -= self._branch_length[j]
                    del self._branch_length[j]
            for children, parent, time in records_in:
                self._children[parent] = children
                self._time[parent] = time
            for children, parent, time in records_in:
                for j in children:
                    bl = time - self._time[j]
                    self._branch_length[j] = bl
                    self._total_branch_length += bl
            # The internal models are now consistent, we can generate the
            # mutations.
            mu = (
                self._total_branch_length * self._scaled_mutation_rate * length
                / self._num_loci)
            num_mutations = self._rng.poisson(mu)
            if num_mutations > 0:
                for j, (node, bl) in enumerate(self._branch_length.items()):
                    branches[j] = node
                    probabilities[j] = bl / self._total_branch_length
                mutation_branches = self._rng.choice(
                    branches, num_mutations, p=probabilities)
                self._apply_mutations(mutation_branches)

    def _apply_mutations(self, branches):
        """
        Adds a single mutation on each of the branches between the specified
        nodes and its parent. Add a 1 to the end of each leaf node
        below this node.
        """
        # First grow our memory if we need to
        new_s = self._num_segregating_sites + len(branches)
        if new_s >= self._max_segregating_sites:
            old_haplotypes = self._haplotype
            n = self._sample_size
            k = self._max_segregating_sites
            self._max_segregating_sites = max(2 * k, new_s + 1)
            self._haplotype = np.empty(
                (self._sample_size, self._max_segregating_sites),
                dtype=(bytes, 1))
            self._haplotype.fill(b"0")
            self._haplotype[0:n, 0:k] = old_haplotypes
        # TODO detect the case of common mutations and deal with them
        # using a different algorithm.
        self._apply_rare_mutations(branches)


    def _apply_rare_mutations(self, branches):
        """
        Apply mutations using an algorithm optimised for when we have only
        a few mutations distributed around the tree.
        """
        for node in branches:
            stack = [node]
            while len(stack) > 0:
                u = stack.pop()
                if u <= self._sample_size:
                    self._haplotype[u - 1, self._num_segregating_sites] = b"1"
                else:
                    stack.extend(self._children[u])
            self._num_segregating_sites += 1

    def get_num_segregating_sites(self):
        return self._num_segregating_sites

    def haplotypes(self):
        for h in self._haplotype:
            yield h[:self._num_segregating_sites]

    def haplotype_strings(self):
        for h in self.haplotypes():
            # We need to convert to str for compatibility with python 3
            yield h.tostring().decode()

class PopulationModel(object):
    """
    Superclass of simulation population models.
    """
    def __init__(self, start_time):
        self.start_time = start_time

    def get_ll_model(self):
        """
        Returns the low-level model corresponding to this population
        model.
        """
        return self.__dict__

class ConstantPopulationModel(PopulationModel):
    """
    Class representing a constant-size population model. The size of this
    is expressed relative to the size of the population at sampling time.
    """
    def __init__(self, start_time, size):
        super(ConstantPopulationModel, self).__init__(start_time)
        self.size = size
        self.type = _msprime.POP_MODEL_CONSTANT


class ExponentialPopulationModel(PopulationModel):
    """
    Class representing an exponentially growing or shrinking population.
    TODO document model.
    """
    def __init__(self, start_time, alpha):
        super(ExponentialPopulationModel, self).__init__(start_time)
        self.alpha = alpha
        self.type = _msprime.POP_MODEL_EXPONENTIAL


