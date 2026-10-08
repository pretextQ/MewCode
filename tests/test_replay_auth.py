"""The evaluation replay must authenticate all operational requests."""
import io
import json
from pathlib import Path

from scripts import m3_replay


def test_replay_sends_token_to_operational_endpoints(tmp_path: Path, monkeypatch, capsys):
    calls = []

    def urlopen(request, timeout):
        path = request.full_url.rsplit("/", 1)[-1]
        calls.append(path)
        if path != "healthz":
            assert request.get_header("X-mewcode-token") == "replay-test-secret"
        body = {
            "healthz": json.dumps({"status": "ok"}),
            "costs": json.dumps({"model": "fake-model", "total": {"tokens_total": 0}}),
            "metrics": "# HELP test Test metric\n",
        }[path]
        response = io.BytesIO(body.encode())
        response.status = 200
        return response

    monkeypatch.setattr(m3_replay.urllib.request, "urlopen", urlopen)
    output = tmp_path / "report.json"
    assert m3_replay.main([
        "--label", "auth-test", "--fingerprint-prefix", "test", "--token", "replay-test-secret",
        "--skip-cases", ",".join(c["case_id"] for c in m3_replay.CASES), "--out", str(output),
    ]) == 0
    assert calls == ["healthz", "costs", "metrics", "costs"]
    report = output.read_text(encoding="utf-8")
    assert json.loads(report)["model"] == "fake-model"
    assert "replay-test-secret" not in report
    assert "replay-test-secret" not in capsys.readouterr().out
