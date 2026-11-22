"""Minimize recorded makespan plus a per-worker charge, in integer milliseconds."""
from heapq import heapreplace

WORKER_MS = 2_000


def _longest_first(jobs, durations, count):
    loads = [(0, idx) for idx in range(count)]
    groups = [[] for _ in range(count)]
    for job in jobs:
        load, idx = loads[0]
        groups[idx].append(job)
        heapreplace(loads, (load + durations[job], idx))
    return max(load for load, _ in loads), groups


def _pack(jobs, durations, count, capacity, budget):
    """Find a feasible allocation, pruning identical loads and bounding CPU use."""
    groups = [[] for _ in range(count)]
    loads = [0] * count

    def search(pos):
        if pos == len(jobs):
            return True
        if budget[0] <= 0:
            return False
        budget[0] -= 1
        job = jobs[pos]
        duration = durations[job]
        tried = set()
        for idx in sorted(range(count), key=loads.__getitem__, reverse=True):
            load = loads[idx]
            if load in tried or load + duration > capacity:
                continue
            tried.add(load)
            loads[idx] += duration
            groups[idx].append(job)
            if search(pos + 1):
                return True
            loads[idx] -= duration
            groups[idx].pop()
        return False

    return groups if search(0) else None


def schedule(selected_numbers, durations_ms):
    jobs = sorted(set(selected_numbers), key=lambda job: (-durations_ms[job], job))
    if not jobs:
        return []
    positive = [job for job in jobs if durations_ms[job] > 0]
    if not positive:
        return [jobs]
    total = sum(durations_ms[job] for job in positive)
    longest = durations_ms[positive[0]]
    best_cost = total + WORKER_MS
    best = [positive.copy()]
    for count in range(2, min(8, len(positive)) + 1):
        overhead = WORKER_MS * count
        if longest + overhead >= best_cost:
            break
        lower = max(longest, (total + count - 1) // count)
        if lower + overhead >= best_cost:
            continue
        makespan, groups = _longest_first(positive, durations_ms, count)
        if len(positive) <= 24 and makespan > lower:
            budget = [6000]
            upper = min(makespan - 1, best_cost - overhead - 1)
            while lower <= upper and budget[0] > 0:
                target = (lower + upper) // 2
                better = _pack(positive, durations_ms, count, target, budget)
                if better is None:
                    lower = target + 1
                else:
                    groups = better
                    makespan = max(sum(durations_ms[j] for j in g) for g in groups)
                    upper = makespan - 1
        if makespan + overhead < best_cost:
            best_cost, best = makespan + overhead, groups
    best[0].extend(jobs[len(positive):])
    return [group for group in best if group]
