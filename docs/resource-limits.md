# Resource limits

[README](../README.md) · [Usage](usage.md)

## CPU and parallel jobs

```bash
decaf ./libs --cpus 8 -j 4
```

- The default CPU budget is the number of cores decaf may use minus one,
  including restrictions from CPU affinity or a container's cpuset.
- `--cpus N` sets the budget explicitly. Workers never exceed that budget.
- On Linux, decaf enforces the budget with CPU affinity inherited by every
  engine JVM.
- Each JVM also receives `-XX:ActiveProcessorCount = cpus ÷ jobs` to size
  its thread pools. On other platforms, this hint is the only CPU limit.
- Maven resolution and downloads use a separate fetch stage and a bounded
  queue, so network waits do not occupy decompiler workers.

## Engine heap budget

```bash
decaf ./libs --memory 8g
decaf ./libs --memory 0       # let the JVM choose its heap
decaf report out --memory    # inspect the largest measured peaks
```

| Setting | Behavior |
| --- | --- |
| Default | 80% of available RAM, including a lower cgroup or parent-cgroup limit |
| `--memory 8g` | An explicit total budget split across jobs |
| `--memory 0` | No decaf heap cap |

- Each job gets one equal share. An archive with at least 3,000 classes gets
  two shares.
- decaf lowers `-j` when a share would provide less than 1 GiB of heap.
- Each heap is its share minus JVM overhead: 300 MB plus 2% of the share.
- With the default budget, no JVM gets more than 25% of available RAM.
  An explicit `--memory` uses the shares without that cap.
- These limits cap Java heaps, not total process memory.

## Memory reports

- Before decompilation, decaf prints the split and warns about artifacts
  likely to need more heap than their share.
- Reports record the heap limit and, on Linux, measured peak memory for each
  engine attempt. Peaks near or below decaf's own memory usage are unknown.
- The run summary shows the largest peak and counts attempts that exhausted
  their heaps. Jars sharing one batch JVM count once.
- `decaf report out --memory` lists the ten JVMs with the largest peaks,
  with one row per batch.

## JVM options and system limits

- Heap options in `JAVA_TOOL_OPTIONS` or `JDK_JAVA_OPTIONS` lose to decaf's
  `-Xmx` setting.
- `_JAVA_OPTIONS` overrides decaf's setting. decaf warns that the budget
  is no longer enforced.
- An initial heap (`-Xms`) above the assigned heap is lowered to that heap.
  If it comes from `_JAVA_OPTIONS`, that JVM runs without a decaf cap instead.
- Under `ulimit -v`, each heap is at most half the address-space limit,
  matching the JVM's own default.
