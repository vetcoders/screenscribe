"""Provider-specific transports whose wire shape differs from the OpenAI-compatible default.

Each submodule owns one provider's REST contract and returns the shared value
types from ``screenscribe.transcribe_types``; routing by endpoint host lives in
``screenscribe.transcribe``.
"""
