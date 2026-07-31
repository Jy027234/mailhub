# Second host example

This example proves the core only needs Host Ports and configuration. It uses
different in-memory identity/action/knowledge adapters and the test-only
sandbox connector. It is not a production provider or a GA portability claim.

`tests/test_second_host.py` exercises the complete local proposal path:
Sandbox sync -> bounded autonomy analysis -> candidate review -> host action.
The test deliberately does not claim a real mailbox, production knowledge
publication, or Provider certification.
