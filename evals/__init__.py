"""The repo's agent-evals subject (RC1-548): mapping quality.

What is measured, on which frozen cases, against which expectations. The
harness itself is agent-evals (pinned by tag in the `evals` dependency
group); the behavior under test is the mapping proposer's flag-don't-guess
contract plus its accuracy on the mappings that have one right answer.
"""
