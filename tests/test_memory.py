import pytest

from decaf import memory
from decaf.memory import FLOOR_MB, SLOT_MIN_MB


@pytest.mark.parametrize(
    "text, mb",
    [("12g", 12288), ("12288m", 12288), ("1.5G", 1536), ("512MiB", 512), ("2gb", 2048),
     ("1t", 1048576), ("0", 0), ("0g", 0), (" 8g ", 8192)],
)
def test_parse_size(text, mb):
    assert memory.parse_size(text) == mb


@pytest.mark.parametrize("text", ["12", "12x", "-1g", "", "g", "12k", "1.2.3g"])
def test_parse_size_rejects(text):
    with pytest.raises(ValueError):
        memory.parse_size(text)  # "12k" rounds to 0 MB: an error, never a silent "no cap"


def _ram(monkeypatch, tmp_path, physical, v2=None, v1=None):
    files = []
    for name, text in (("v2", v2), ("v1", v1)):
        p = tmp_path / name
        if text is not None:
            p.write_text(text)
        files.append(p)
    monkeypatch.setattr(memory, "_physical_ram", lambda: physical)
    monkeypatch.setattr(memory, "_CGROUP_FILES", tuple(files))
    return memory.total_ram_mb()


def test_total_ram_is_the_physical_ram(monkeypatch, tmp_path):
    assert _ram(monkeypatch, tmp_path, 16 * 2**30) == 16384


def test_total_ram_takes_a_lower_cgroup_limit(monkeypatch, tmp_path):
    assert _ram(monkeypatch, tmp_path, 16 * 2**30, v2="4294967296\n") == 4096
    assert _ram(monkeypatch, tmp_path, 16 * 2**30, v1="2147483648\n") == 2048


def test_total_ram_ignores_unlimited_cgroups(monkeypatch, tmp_path):
    assert _ram(monkeypatch, tmp_path, 16 * 2**30, v2="max\n", v1="9223372036854771712\n") == 16384


def test_total_ram_ignores_junk_and_reports_unknown(monkeypatch, tmp_path):
    assert _ram(monkeypatch, tmp_path, 16 * 2**30, v2="junk") == 16384
    assert _ram(monkeypatch, tmp_path, None) is None
    assert _ram(monkeypatch, tmp_path, None, v2="4294967296") == 4096


def test_physical_ram_is_known_here():
    assert memory._physical_ram() > 0  # sysconf on Linux and macOS, GlobalMemoryStatusEx on Windows


def test_default_budget_is_80_percent(monkeypatch):
    monkeypatch.setattr(memory, "total_ram_mb", lambda: 10000)
    assert memory.default_budget_mb() == 8000
    monkeypatch.setattr(memory, "total_ram_mb", lambda: None)
    assert memory.default_budget_mb() is None


def test_default_cap_is_the_jvm_default_heap(monkeypatch):
    monkeypatch.setattr(memory, "total_ram_mb", lambda: 10000)
    assert memory.default_cap_mb() == 2500  # 25% of the RAM: the JVM's own default maximum heap
    monkeypatch.setattr(memory, "total_ram_mb", lambda: None)
    assert memory.default_cap_mb() is None


def test_heap_in_leaves_the_jvm_overhead():
    assert memory.heap_in(SLOT_MIN_MB) == FLOOR_MB  # 1351 - 300 - 27
    assert memory.heap_in(SLOT_MIN_MB - 1) < FLOOR_MB
    assert memory.heap_in(24576) == 23785  # 24576 - 300 - 491: the G1 tables grow with the heap


@pytest.mark.parametrize(
    "budget, heap, whale",
    [(6553, 1306, 2911), (13107, 2911, 6121), (26214, 6122, 12544)],  # 80% of 8, 16, 32 GiB
)
def test_plan_examples_with_four_jobs(budget, heap, whale):
    p = memory.plan(budget, 4)
    assert (p.jobs, p.heap_mb(1), p.heap_mb(2)) == (4, heap, whale)


def test_plan_clamps_jobs_to_the_floor():
    assert memory.plan(6553, 8).jobs == 4  # 6553 // 1351
    p = memory.plan(SLOT_MIN_MB, 4)
    assert (p.jobs, p.heap_mb(1)) == (1, 1024)


def test_plan_whale_alone_in_one_job_gets_one_slot():
    p = memory.plan(2000, 1)
    assert p.heap_mb(2) == p.heap_mb(1) == 1660  # 2000 - 300 - 40


def test_plan_small_auto_budget_keeps_one_job():
    assert (memory.plan(800, 4).jobs, memory.plan(800, 4).heap_mb(1)) == (1, 484)
    assert memory.plan(400, 4).heap_mb(1) == 256


def test_plan_without_a_cap():
    p = memory.plan(0, 4)
    assert (p.budget_mb, p.jobs, p.heap_mb(1), p.heap_mb(2)) == (0, 4, None, None)
    assert memory.NO_CAP.heap_mb(2) is None
    assert memory.plan(0, 4, cap_mb=8022) == memory.plan(0, 4)  # no budget: the cap goes too


def test_plan_cap_stops_each_heap():
    p = memory.plan(25670, 4, cap_mb=8022)  # 31.3 GiB of RAM: the auto budget and the auto cap
    assert (p.jobs, p.heap_mb(1), p.heap_mb(2)) == (4, 5989, 8022)  # the whale: 12278 without the cap
    p = memory.plan(25670, 2, cap_mb=8022)
    assert (p.heap_mb(1), p.heap_mb(2)) == (8022, 8022)


def test_plan_cap_below_the_floor_wins():
    p = memory.plan(1638, 4, cap_mb=512)  # 2 GiB of RAM: the JVM default, as in 1.10.0
    assert (p.jobs, p.heap_mb(1), p.heap_mb(2)) == (1, 512, 512)


def test_estimate_heap():
    assert memory.estimate_heap_mb("vineflower", 64 * 2**20) == 73 + 48 * 64
    assert memory.estimate_heap_mb("cfr", 10 * 2**20) == 85 + 22 * 10
    assert memory.estimate_heap_mb("vineflower", 2**19) == 97  # 73 + 24
    assert memory.estimate_heap_mb("jd", 64 * 2**20) is None


def test_budget_line():
    assert memory.budget_line(memory.plan(26214, 4)) == (
        "engine memory: up to 25.6 GB (4 jobs, 6.0 GB heap each; a whale gets 12.2 GB)"
    )
    assert memory.budget_line(memory.plan(2000, 1)) == "engine memory: up to 2.0 GB (1 job, 1.6 GB heap)"
    assert memory.budget_line(memory.plan(25670, 4, cap_mb=8022)) == (
        "engine memory: up to 25.1 GB (4 jobs, 5.8 GB heap each; a whale gets 7.8 GB)"
    )
    assert memory.budget_line(memory.plan(25670, 2, cap_mb=8022)) == (
        "engine memory: up to 25.1 GB (2 jobs, 7.8 GB heap each)"
    )
    assert memory.budget_line(memory.plan(0, 4)) == "engine memory: no cap (--memory 0)"
    assert memory.budget_line(memory.plan(0, 4), ram_unknown=True) == (
        "engine memory: no cap (RAM size unknown; set --memory)"
    )


def test_heap_env_warnings():
    env = {
        "JAVA_TOOL_OPTIONS": "-Xmx4g -Dfoo=1",
        "JDK_JAVA_OPTIONS": "-XX:MaxRAMPercentage=50",
        "_JAVA_OPTIONS": "-XX:MaxHeapSize=2g -XX:MaxRAM=8g",
        "PATH": "-Xmx1g",
    }
    assert memory.heap_env_warnings(env) == [
        "JAVA_TOOL_OPTIONS sets the heap (-Xmx4g); decaf's --memory heap takes precedence (--memory 0 keeps yours)",
        "JDK_JAVA_OPTIONS sets the heap (-XX:MaxRAMPercentage=50); decaf's --memory heap takes precedence"
        " (--memory 0 keeps yours)",
        "_JAVA_OPTIONS sets the heap (-XX:MaxHeapSize=2g) and overrides decaf's --memory shares:"
        " the budget is not enforced",
    ]


def test_heap_env_warnings_ignore_other_options():
    env = {"JAVA_TOOL_OPTIONS": "-Dfile.encoding=UTF-8", "_JAVA_OPTIONS": "-XX:MaxRAM=8g"}
    assert memory.heap_env_warnings(env) == []
