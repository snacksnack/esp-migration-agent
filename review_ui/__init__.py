"""Review-gate UI backend (RC1-560): a thin FastAPI layer over the gate.

The UI is a front end over existing gate semantics, never a second
implementation: approval goes through mapping.gate.approve (refusals surface
verbatim), resolutions edit the draft spec YAML on disk through the pydantic
model, and the discovery charts read the same DiscoveryReport the report CLI
renders. Nothing here decides anything the CLI would not.
"""
