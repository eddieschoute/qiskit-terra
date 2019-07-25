from typing import Callable, Mapping, Iterable, List, Optional, Tuple

import networkx as nx

from qiskit.dagcircuit import DAGCircuit, DAGNode
from qiskit.transpiler.passes.mapping.placement import Placement
from qiskit.transpiler.passes.mapping.size import SizeMapper, Reg, ArchNode, logger
from qiskit.transpiler.routing import Swap


class ExtensionSizeMapper(SizeMapper[Reg, ArchNode]):
    def __init__(self, arch_graph: nx.DiGraph,
                 arch_permuter: Callable[[Mapping[ArchNode, ArchNode]],
                                         Iterable[Swap[ArchNode]]],
                 lookahead: bool = False) -> None:
        super().__init__(arch_graph, arch_permuter)
        self.lookahead = lookahead

    def size_map(self,
                 circuit: DAGCircuit,
                 current_mapping: Mapping[Reg, ArchNode],
                 binops: List[DAGNode]) -> Mapping[Reg, ArchNode]:
        """Place the cheapest gate and try to extend the placement with further good placements."""
        # Peel off the first layer of operations for the circuit
        # so that we can assign operations to the architecture.
        remaining_arch = self.arch_graph.copy()
        current_placement: Optional[Placement] = None

        def placement_score(place: Tuple[Placement[Reg, ArchNode], DAGNode]) -> int:
            """Returns a score for this placement, the higher the better."""
            placement, binop = place
            saved_now = self.saved_gates((placement, [binop]), current_placement, current_mapping)
            if not self.lookahead:
                return saved_now

            ###
            # Find the next gate that will be placed, and where.
            # See if the next placement will be improved by this placement.
            ###
            # TODO: This is now O(n)
            remaining_binops = binops[:]
            remaining_binops.remove(binop)
            new_remaining_arch = \
                remaining_arch.subgraph(node for node in remaining_arch.nodes()
                                        if node not in placement.mapped_to.values())
            if remaining_binops and len(new_remaining_arch.edges()) > 0:
                cur_place: Placement[Reg, ArchNode]
                if current_placement is None:
                    cur_place = Placement({}, {})
                else:
                    cur_place = current_placement

                # The extra cost incurred by placing 'placement'.
                place_cost_diff = self.placement_cost(placement + cur_place) \
                                  - self.placement_cost(cur_place)

                def placement_diff(place: Tuple[Placement[Reg, ArchNode], DAGNode]) -> int:
                    """Approximates saved_gates but it easier to compute.

                    It computes the cost of placing a given gate plus (separately) the 'placement'
                    gate versus the cost of placing everything together.
                    This does not require a lookahead."""
                    return self.placement_cost(place[0] + cur_place) + place_cost_diff \
                           - self.placement_cost(place[0] + placement + cur_place)

                next_placement = self._inner_simple(remaining_binops,
                                                    current_mapping,
                                                    new_remaining_arch,
                                                    placement_diff)
                diff_next = placement_diff(next_placement)
            else:
                diff_next = 0

            # Is it better to place the gate now or wait until the next iteration?
            # If the result is zero or less then it's not worse to place the gate now.
            return saved_now + diff_next

        placed_gates = 0
        total_gates = len(binops)
        while binops and remaining_arch.edges():
            max_max_placement: Optional[Tuple[Placement[Reg, ArchNode], DAGNode]] = None
            for binop in binops:
                binop_map: Mapping[Reg, ArchNode] = {
                    qarg: current_mapping[qarg]
                    for qarg in binop.qargs
                    }

                # Try all edges and find the minimum cost placement.
                all_edges = {e for directed_edge in remaining_arch.edges()
                             for e in [directed_edge, tuple(reversed(directed_edge))]}
                placements = ((Placement(binop_map, dict(zip(binop.qargs, edge))), binop)
                              for edge in all_edges)

                # Find the cost of placing this gate given the current placement,
                # versus a placement without the current placement.
                # If this is positive it means that placing this gate now is advantageous.
                max_placement = max(placements, key=placement_score)

                if max_max_placement is None:
                    max_max_placement = max_placement
                else:
                    max_max_placement = max(max_max_placement, max_placement, key=placement_score)

            if max_max_placement is None:
                raise RuntimeError("The max_max_placement is None. Was binops_qargs empty?")

            # Place the cheapest binops, but only if it is advantageous by the placement_score.
            if current_placement is None:
                # Always place at least one binop.
                current_placement = max_max_placement[0]
            else:
                score = placement_score(max_max_placement)
                if score < 0:
                    # There are no advantageous gates to place left.
                    break
                if score > 0:
                    logger.debug(f"Saved cost! Placement score: {score}")
                current_placement += max_max_placement[0]

            # Remove the placed binop from datastructure.
            binops.remove(max_max_placement[1])
            # The nodes are now in use and can no longer be used for anything else.
            remaining_arch.remove_nodes_from(max_max_placement[0].mapped_to.values())
            placed_gates += 1

        logger.debug(f"Number of gates placed: {placed_gates}/{total_gates}")
        if current_placement is None:
            raise RuntimeError("The current_placement is None. Somehow it did not get set.")
        return current_placement.mapped_to