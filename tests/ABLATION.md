<!-- Copyright (c) 2026 Roronoa & Haruka · Documentation licensed CC BY-NC-SA 4.0 -->
# Ablation Report

The release suite includes mutation-style ablation checks. Each experiment copies `code/` and `test_bridge.py` into a temporary directory, removes one protection, and requires the bridge tests to fail. Production files are never edited by the experiment.

Run the complete suite:

```bash
PYTHONPATH=code python -m unittest discover -s tests -v
```

Run only the ablations:

```bash
PYTHONPATH=code python tests/test_ablations.py
```

| Removed protection | Failure the tests must detect |
|---|---|
| Callback signature comparison | A forged `msg_signature` is accepted |
| CorpID trailer comparison | Ciphertext for another enterprise decrypts successfully |
| Exact AgentID check | A callback with no matching application route enters the queue |
| Hardened XML parser | An XML entity declaration is expanded |
| Persistent inbound de-duplication | The same application `MsgId` is accepted twice |
| Application target allowlist | An unapproved enterprise UserID reaches the send path |
| Customer binding | A different `external_userid` reaches the customer-service send path |
| Cumulative five-message budget | A reply reserves more slots than remain |
| Source-aware reply routing | A customer-service envelope no longer selects `kf/send_msg` |
| Pending reply persistence | A retry regenerates or loses the reply already produced |
| Sent-chunk checkpoint | A retry starts before the last confirmed chunk |

Release result on 2026-09-03: **11 of 11 ablations were detected**. The unmodified suite passed after the experiment.

Documentation © 2026 Roronoa & Haruka · From Raincove ♡ · [CC BY-NC-SA 4.0](../LICENSE)
