"""Static checks on the development launcher.

These are guards against one defect, and it is worth naming because it did not
look like a defect. The stack could be started two ways - everything in Docker,
or the API on the host so the assistant CLIs are reachable - and each way had
its own port. Two API processes can share this PostgreSQL and not share a
filesystem, so a document uploaded through one is registered in a row the other
can read while its bytes sit where only the first can see them. `POST /process`
queues the work for a worker to execute, so what broke across the pair was
uploading and processing, not only downloading a report; and the symptom was a
404 on an artefact the screen had just listed.

One port, one process at a time. What follows pins that, because the pair is
easy to reintroduce - a second port is the obvious way to run two things.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# Resolved from this file rather than the working directory, like the n8n
# workflow checks: the suite runs from the repository root in CI and from /app
# inside the image, where `scripts/` is deliberately not shipped.
SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
DEV = SCRIPTS / "dev.ps1"
DEMO = SCRIPTS / "demo.ps1"

pytestmark = pytest.mark.skipif(
    not DEV.exists(),
    reason="the development scripts are not part of the runtime image",
)


def dev() -> str:
    return DEV.read_text(encoding="utf-8")


def test_both_modes_serve_one_address() -> None:
    """The address is declared once and every mode announces that variable."""
    text = dev()
    assert text.count('$url = "http://127.0.0.1:8000/ui/dossiers"') == 1
    # Two announcements - one per mode - and both interpolate `$url` rather
    # than spelling an address out, which is how they drifted apart before.
    assert len(re.findall(r'Write-Host "Listo\.\s+\$url"', text)) == 2
    # The host process listens where the container publishes, and nowhere else.
    assert text.count("--port 8000") == 1
    assert set(re.findall(r"--port (\d+)", text)) == {"8000"}
    # Three loopback ports, one per service, and each is somebody else's: the
    # application, the source simulator, PostgreSQL. A fourth appearing means a
    # second way in was added, which is what this file is here to notice.
    assert set(re.findall(r"127\.0\.0\.1:(\d+)", text)) == {"8000", "8080", "55432"}


def test_the_port_is_freed_before_anything_binds_it() -> None:
    """Container mode has to clear a previous host run, or Compose cannot bind.

    Without this the failure is "port is already allocated", which reads as a
    broken stack rather than as the other mode still running.
    """
    text = dev()
    free = text.index('Stop-HostProcesses\n\nWrite-Host "Levantando')
    bind = text.index("docker compose up -d --wait @services")
    assert free < bind


def test_stopping_a_process_that_already_exited_is_not_an_error() -> None:
    """Every `Stop-Process` tolerates an id that has gone.

    The worker runs as a launcher and its child, both carrying
    `iep.worker.runner` on their command line, so stopping the first stops the
    second - and the loop then asks a dead id to die. As a cmdlet error under
    `$ErrorActionPreference = "Stop"` that is terminating, and the script
    aborted before starting anything, having stopped the containers first.

    The state being asked for is "not running", which a process that already
    exited satisfies.
    """
    offenders = [
        line.strip()
        for line in dev().splitlines()
        if "Stop-Process" in line
        and "ErrorAction" not in line
        and not line.lstrip().startswith("#")
    ]
    assert not offenders, "Stop-Process without -ErrorAction:\n  " + "\n  ".join(offenders)


def test_host_mode_stops_the_container_api_and_worker() -> None:
    """Both of them. The worker is the one that is easy to forget.

    An orphaned container worker keeps leasing jobs and writing their output to
    a volume the host process cannot read, so roughly half of the work lands
    somewhere invisible - intermittently, depending on which process won the
    lease.
    """
    assert "docker compose stop api worker" in dev()


def test_it_does_not_promise_readiness_before_it_can_bind() -> None:
    """ "Listo" after the check, not before it.

    uvicorn logs a bind failure as an ERROR line and then exits 0. The banner
    was printed before it started, so a run whose port was already taken said
    "Listo.  http://127.0.0.1:8000/ui/dossiers", started nothing, and reported
    success - and the request that followed was served by a process from
    before the change being tested, which is the worst version of this because
    it looks like the change did not work.
    """
    text = dev()
    check = text.index("if (-not (Wait-ForFreePort))")
    banner = text.index(
        'Write-Host "Listo.  $url"\nWrite-Host "  modelos: los locales de Ollama, mas'
    )
    assert check < banner, "el script promete antes de comprobar que puede enlazar"
    # And the failure says which pid, because the answer depends on whether
    # that process still exists.
    assert "$holder" in text


def test_host_mode_sets_the_embedding_provider() -> None:
    """The one variable whose absence is silent.

    Vector search only compares vectors made with the same configuration. A
    host process left on the default hashing baseline finds nothing at all in a
    corpus indexed with a learned model, and the honest failure - an empty
    answer to a question the evidence does answer - looks like bad retrieval
    rather than like a misconfiguration.
    """
    text = dev()
    assert '$env:IEP_EMBEDDING_PROVIDER = "ollama"' in text
    assert '$env:IEP_OLLAMA_EMBEDDING_MODEL = "qwen3-embedding:4b"' in text
    # And the two host processes agree on where the bytes go, which is the same
    # defect as the two ports one level down: the API writes an upload and the
    # worker reads it.
    assert '$env:IEP_STORAGE_ROOT = "var/objects"' in text
    assert '$env:IEP_REPORT_ROOT = "var/reports"' in text
    # Those directories exist before either process is told to use them.
    assert 'New-Item -ItemType Directory -Force -Path "var\\objects", "var\\reports"' in text


def test_the_worker_inherits_the_configuration_it_cannot_disagree_about() -> None:
    """Read back from the environment, never written a second time.

    Two of these were spelled out twice - once for the API, once in the string
    that launches the worker - so a change to one was a change to one. A worker
    indexing at a width the API does not search at loses evidence silently:
    vector search compares only what was made with the same configuration, so
    the symptom is an honest empty answer to a question the corpus does answer.
    """
    text = dev()
    for variable in (
        "IEP_DATABASE_URL",
        "IEP_STORAGE_ROOT",
        "IEP_REPORT_ROOT",
        "IEP_REGISTRY_API_BASE_URL",
        "IEP_EMBEDDING_PROVIDER",
        "IEP_OLLAMA_EMBEDDING_MODEL",
        # Tesseract was prepended to this process's PATH; without it the worker
        # accepts a scan and then cannot read it.
        "PATH",
    ):
        assert f'    "{variable}"' in text, f"{variable} is not passed to the worker"
    assert "[Environment]::GetEnvironmentVariable($_)" in text

    # The invariant behind all of it: no `IEP_*` variable is assigned twice, so
    # there is one place per value and nothing to keep in step by hand.
    assigned = re.findall(r"\$env:(IEP_\w+)\s*=", text)
    duplicated = {name for name in assigned if assigned.count(name) > 1}
    assert not duplicated, f"assigned in more than one place: {sorted(duplicated)}"


def test_only_the_worker_is_matched_by_its_command_line() -> None:
    """A shell is never identified by what its command line mentions.

    The worker runs in a window opened with `-NoExit`, so stopping the worker
    has to close the window too or one empty prompt accumulates per run. The
    obvious way to find it - a `powershell.exe` whose command line mentions
    `iep.worker.runner` - matches any shell that mentions the worker, including
    the one running the script. That was not hypothetical: a diagnostic shell
    here matched on exactly that string, and the regression closes windows that
    belong to somebody else.

    So the window is only ever reached as the parent of a matched `python.exe`.
    """
    text = dev()
    assert "Name='python.exe'" in text
    assert "Name='powershell.exe'" not in text, "shells must not be matched by a CIM filter"
    assert "ProcessId=$($_.ParentProcessId)" in text
    # And the parent is confirmed before anything is sent to it, in case the id
    # was recycled after the real parent exited.
    assert '$parent.Name -eq "powershell.exe"' in text
    assert '$parent.CommandLine -like "*iep.worker.runner*"' in text


def test_host_mode_brings_the_seeded_corpus_with_it() -> None:
    """Copied, not assumed: the database is shared and the filesystem is not.

    Without this the evidence viewer and the report answer 404 for every
    document seeded in container mode - on the same URL that had just worked,
    which is precisely the confusion this script exists to remove.
    """
    text = dev()
    assert "docker compose cp api:/var/lib/iep/objects/. var\\objects" in text
    assert "docker compose cp api:/var/lib/iep/reports/. var\\reports" in text
    # The trailing `/.` is the whole point: it merges into an existing
    # directory. Without it a second run nests var/objects/objects and the
    # evidence quietly stops being found.
    assert "cp api:/var/lib/iep/objects var" not in text


@pytest.mark.parametrize("script", [DEV, DEMO])
def test_no_native_command_has_its_stderr_redirected(script: Path) -> None:
    """In PowerShell 5.1 that is a terminating error, not a quieter command.

    Redirecting a native executable's stderr inside PowerShell wraps each line
    it writes in a NativeCommandError record, and with
    `$ErrorActionPreference = "Stop"` that record is terminating. `docker
    compose cp` writes its ordinary progress to stderr, so `cp ... 2>&1` made
    the script exit - reporting success, with the API never started and every
    container stopped. It printed its way out of its own script.

    Redirections belong on cmdlets, where they behave; a native command's
    stderr is left alone and its exit code is what gets checked.
    """
    if not script.exists():
        pytest.skip(f"{script.name} is not present")
    lines = script.read_text(encoding="utf-8").splitlines()
    offenders = [
        f"{script.name}:{number}: {line.strip()}"
        for number, line in enumerate(lines, start=1)
        if not line.lstrip().startswith("#")
        and re.search(
            r"^\s*(?:&\s*)?(?:docker|python|pip|curl|git|npm|[\w.\\/-]+\.exe)\b.*2>", line
        )
    ]
    assert not offenders, "native stderr redirected:\n  " + "\n  ".join(offenders)


@pytest.mark.parametrize("script", [DEV, DEMO])
def test_no_script_destroys_the_demo(script: Path) -> None:
    """Stopping is not deleting.

    The seeded corpus is minutes of processing and the point of the
    demonstration. `down -v` removes the volumes with it, so no script here
    offers that as a convenience; `docker compose stop` is what stopping means.
    """
    if not script.exists():
        pytest.skip(f"{script.name} is not present")
    text = script.read_text(encoding="utf-8")
    lowered = text.lower()
    for destructive in ("down -v", "down --volumes", "-v down", "system prune", "volume rm"):
        assert destructive not in lowered, f"{script.name} would delete the demo: {destructive}"
    # `docker compose down` on its own keeps volumes, so it is allowed to be
    # mentioned as a stop instruction - but not run by a script whose job is to
    # start things.
    assert not re.search(r"^\s*docker compose (--profile \S+ )?down\b", text, re.MULTILINE)
