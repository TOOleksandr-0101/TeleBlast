"""Regression tests for the worker cost used by the external scorer."""

import random
import unittest

from scheduler import schedule


def score(workers, durations):
    loads = [sum(durations[job] for job in worker) for worker in workers if worker]
    return max(loads, default=0) + 2000 * len(loads)


def optimal_cost(durations):
    """Enumerate every unlabeled partition, including every allowed worker count."""
    loads = []

    def visit(index):
        if index == len(durations):
            return max(loads, default=0) + 2000 * len(loads)
        cost = float("inf")
        for worker in range(len(loads)):
            loads[worker] += durations[index]
            cost = min(cost, visit(index + 1))
            loads[worker] -= durations[index]
        if len(loads) < 8:
            loads.append(durations[index])
            cost = min(cost, visit(index + 1))
            loads.pop()
        return cost

    return visit(0)


class TestScheduler(unittest.TestCase):
    def test_zero_duration_tests_are_retained(self):
        self.assertEqual(schedule([], {}), [])
        self.assertEqual(schedule([6, 1, 9], {1: 0, 6: 0, 9: 0}), [[1, 6, 9]])
        jobs = {8: 9000, 9: 9000, 11: 0}
        workers = schedule(jobs, jobs)
        self.assertEqual(sorted(j for group in workers for j in group), sorted(jobs))
        self.assertEqual(score(workers, jobs), 13000)

    def test_overhead_can_make_one_worker_cheaper(self):
        for duration, expected_count in [(1999, 1), (2000, 1), (2001, 2)]:
            jobs = {1: duration, 2: duration}
            with self.subTest(duration=duration):
                self.assertEqual(len(schedule(jobs, jobs)), expected_count)

    def test_longest_first_greedy_trap(self):
        # LPT's two loads are 5600 and 4000. The optimum splits the two
        # 2400 ms jobs from the three 1600 ms jobs, giving both workers 4800.
        durations = [2400, 2400, 1600, 1600, 1600]
        workers = schedule(range(len(durations)), durations)
        self.assertEqual(score(workers, durations), 8800)
        self.assertEqual(len(workers), 2)

    def test_deterministic_sparse_ids(self):
        durations = {20: 4000, 13: 4000, 99: 3000, 4: 3000, 37: 0}
        expected = schedule(durations, durations)
        self.assertEqual(schedule([99, 4, 13, 20, 37, 99], durations), expected)
        self.assertEqual(sorted(j for group in expected for j in group), sorted(durations))

    def test_eight_worker_limit(self):
        durations = [50000] * 9
        workers = schedule(range(9), durations)
        # A ninth worker would reduce the score, but the best legal cost is
        # two jobs on the fullest worker, with five paid workers in total.
        self.assertEqual(len(workers), 5)
        self.assertEqual(score(workers, durations), 110000)
        self.assertEqual(len(schedule(range(8), durations)), 8)

    def test_short_instances_match_exhaustive_optimum(self):
        rng = random.Random(83001)
        for count in range(1, 9):
            for _ in range(10):
                durations = [rng.randrange(0, 40) * 207 for _ in range(count)]
                with self.subTest(durations=durations):
                    workers = schedule(range(count), durations)
                    self.assertEqual(sorted(j for group in workers for j in group), list(range(count)))
                    self.assertEqual(score(workers, durations), optimal_cost(durations))
