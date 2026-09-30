#!/usr/bin/env python3
"""Create the public numeric usage record without retaining provider output."""
import argparse
import json
import os
import re
import signal
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

MAX_SOURCE_BYTES = 16 * 1024 * 1024
MAX_JSONL_LINE_BYTES = 1024 * 1024
MAX_TOKEN_COUNT = 2**63 - 1
TOKEN_FIELDS = ("input_tokens", "cached_input_tokens", "cache_creation_input_tokens",
                "output_tokens", "reasoning_output_tokens")
ALIAS = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,63}$")
NAME = re.compile(r"^[A-Za-z0-9_.-]+$")
SHA = re.compile(r"^[0-9a-fA-F]{40,64}$")


class ExpectedSourceError(Exception):
    """Untrusted provider data could not supply valid usage."""


def token(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_TOKEN_COUNT:
        raise ExpectedSourceError("invalid token count")
    return value


def add_tokens(total, addition):
    value = token(total) + token(addition)
    if value > MAX_TOKEN_COUNT:
        raise ExpectedSourceError("token total is too large")
    return value


def observed_at():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def metadata(args, provider):
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    parts = repository.split("/", 1)
    if len(parts) != 2 or not all(NAME.fullmatch(part) for part in parts):
        raise ValueError("GITHUB_REPOSITORY must be owner/name")
    head_sha = os.environ.get("VV_HEAD_SHA") or os.environ.get("GITHUB_SHA", "")
    if not SHA.fullmatch(head_sha):
        raise ValueError("the full GitHub head revision is required")
    try:
        run_id = int(os.environ["GITHUB_RUN_ID"])
        run_attempt = int(os.environ["GITHUB_RUN_ATTEMPT"])
    except (KeyError, ValueError):
        raise ValueError("numeric GitHub run provenance is required") from None
    if run_id < 1 or run_attempt < 1:
        raise ValueError("numeric GitHub run provenance must be positive")
    if not NAME.fullmatch(args.role) or not NAME.fullmatch(args.job_key):
        raise ValueError("role and job key must be simple identifiers")
    account = args.account_alias or None
    if account is not None and not ALIAS.fullmatch(account):
        raise ValueError("account alias must be a logical identifier of at most 64 characters")
    return {
        "schema_version": 1,
        "provider": provider,
        "role": args.role,
        "account_alias": account,
        "owner": parts[0],
        "repository": repository,
        "head_sha": head_sha.lower(),
        "run_id": run_id,
        "run_attempt": run_attempt,
        "job_key": args.job_key,
        "observed_at": observed_at(),
    }


def record(args, provider, status, reason, usage):
    result = metadata(args, provider)
    result.update({"status": status, "status_reason": reason, "usage": usage})
    return result


def write_record(path_text, value):
    path = Path(path_text)
    if path.name != "usage.json":
        raise ValueError("usage output must be named usage.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".usage-", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def unavailable(args, provider, reason):
    return record(args, provider, "unavailable", reason, None)


def read_execution_file(path_text):
    if not path_text:
        raise FileNotFoundError
    path = Path(path_text)
    try:
        details = path.lstat()
    except FileNotFoundError:
        raise
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
        raise ExpectedSourceError("execution source must be a regular file")
    if details.st_size > MAX_SOURCE_BYTES:
        raise ExpectedSourceError("execution source is too large")
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ExpectedSourceError("execution source is not JSON") from error


def claude_usage(usage):
    if not isinstance(usage, dict):
        raise ExpectedSourceError("result has no usage object")
    return {
        "input_tokens": token(usage.get("input_tokens")),
        "cached_input_tokens": token(usage.get("cache_read_input_tokens")),
        "cache_creation_input_tokens": token(usage.get("cache_creation_input_tokens")),
        "output_tokens": token(usage.get("output_tokens")),
        "reasoning_output_tokens": None,
    }


def collect_claude(args):
    if args.source_ran == "false":
        return unavailable(args, "anthropic", "not_run")
    try:
        messages = read_execution_file(args.source_file)
    except FileNotFoundError:
        return unavailable(args, "anthropic", "no_artifact")
    except OSError:
        return unavailable(args, "anthropic", "no_artifact")
    except ExpectedSourceError:
        return unavailable(args, "anthropic", "invalid_usage")
    if not isinstance(messages, list):
        return unavailable(args, "anthropic", "invalid_usage")
    results = [message for message in messages
               if isinstance(message, dict) and message.get("type") == "result"]
    if not results:
        return unavailable(args, "anthropic", "no_final_usage")
    final = results[-1]
    if final.get("usage") is None:
        return unavailable(args, "anthropic", "no_final_usage")
    try:
        usage = claude_usage(final.get("usage"))
    except ExpectedSourceError:
        return unavailable(args, "anthropic", "invalid_usage")
    if final.get("subtype") != "success" or final.get("is_error") is not False:
        return record(args, "anthropic", "partial", "provider_failed", usage)
    return record(args, "anthropic", "complete", None, usage)


def empty_codex_usage():
    return {name: (None if name == "cache_creation_input_tokens" else 0) for name in TOKEN_FIELDS}


def codex_event_usage(event):
    if not isinstance(event, dict) or event.get("type") != "turn.completed":
        return None
    source = event.get("usage")
    if not isinstance(source, dict):
        raise ExpectedSourceError("completed turn has no usage object")
    return {
        "input_tokens": token(source.get("input_tokens")),
        "cached_input_tokens": token(source.get("cached_input_tokens")),
        "cache_creation_input_tokens": None,
        "output_tokens": token(source.get("output_tokens")),
        "reasoning_output_tokens": token(source.get("reasoning_output_tokens")),
    }


def consume_codex_line(raw, totals):
    try:
        event = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ExpectedSourceError("invalid JSONL event") from error
    addition = codex_event_usage(event)
    if addition is None:
        return False
    updated = dict(totals)
    for name in TOKEN_FIELDS:
        if name != "cache_creation_input_tokens":
            updated[name] = add_tokens(totals[name], addition[name])
    totals.update(updated)
    return True


def stream_codex(stdout):
    totals = empty_codex_usage()
    valid_events = 0
    invalid = False
    while True:
        raw = stdout.readline(MAX_JSONL_LINE_BYTES + 1)
        if not raw:
            break
        if len(raw) > MAX_JSONL_LINE_BYTES:
            invalid = True
            while raw and not raw.endswith(b"\n"):
                raw = stdout.readline(MAX_JSONL_LINE_BYTES + 1)
            continue
        try:
            valid_events += int(consume_codex_line(raw, totals))
        except ExpectedSourceError:
            invalid = True
    return totals, valid_events, invalid


def shell_status(returncode, forwarded_signal):
    if forwarded_signal:
        return 128 + forwarded_signal
    return 128 - returncode if returncode < 0 else returncode


def collect_codex(args):
    command = list(args.command)
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        raise ValueError("codex collector needs a command after --")
    print("usage collector: provider run started", flush=True)
    try:
        child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        value = unavailable(args, "openai", "provider_failed")
        write_record(args.output, value)
        print("usage collector: provider process could not start", flush=True)
        return 127
    forwarded = {"signal": 0}

    def forward(signum, _frame):
        forwarded["signal"] = signum
        if child.poll() is None:
            try:
                os.killpg(child.pid, signum)
            except ProcessLookupError:
                pass

    old_handlers = {number: signal.signal(number, forward) for number in (signal.SIGINT, signal.SIGTERM)}
    try:
        totals, valid_events, invalid = stream_codex(child.stdout)
        returncode = child.wait()
    finally:
        for number, handler in old_handlers.items():
            signal.signal(number, handler)
        child.stdout.close()
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGTERM)
            child.wait()
    exit_status = shell_status(returncode, forwarded["signal"])
    if valid_events:
        if exit_status:
            value = record(args, "openai", "partial", "provider_failed", totals)
        elif invalid:
            value = record(args, "openai", "partial", "invalid_usage", totals)
        else:
            value = record(args, "openai", "complete", None, totals)
    else:
        reason = "provider_failed" if exit_status else ("invalid_usage" if invalid else "no_final_usage")
        value = unavailable(args, "openai", reason)
    write_record(args.output, value)
    print("usage collector: provider run ended; metrics %s" % value["status"], flush=True)
    return exit_status


def parser():
    result = argparse.ArgumentParser()
    result.add_argument("--output", required=True)
    result.add_argument("--role", required=True)
    result.add_argument("--job-key", required=True)
    result.add_argument("--account-alias", default="")
    modes = result.add_subparsers(dest="mode", required=True)
    claude = modes.add_parser("claude")
    claude.add_argument("--source-file", default="")
    claude.add_argument("--source-ran", choices=("true", "false"), default="true")
    codex = modes.add_parser("codex")
    codex.add_argument("command", nargs=argparse.REMAINDER)
    return result


def main():
    args = parser().parse_args()
    try:
        if args.mode == "claude":
            write_record(args.output, collect_claude(args))
            return 0
        return collect_codex(args)
    except ValueError as error:
        parser().error(str(error))


if __name__ == "__main__":
    sys.exit(main())
