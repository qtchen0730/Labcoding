"""No service or database needed for strict upstream error classification."""
import json
import unittest
from .context_errors import context_error


class ContextErrorTests(unittest.TestCase):
    def test_known_code(self):
        self.assertIsNone(context_error(b'{}'))
        value = context_error(b'{"error":{"type":"exceed_context_size_error","message":"too long"}}')
        self.assertIsNone(value["input_tokens"])

    def test_llama_numeric_context(self):
        value = context_error(json.dumps({"error": {"message":
            "request (28000 tokens) exceeds the available context size (26368 tokens), try increasing it"}}))
        self.assertEqual((value["input_tokens"], value["context_limit"]), (28000, 26368))

    def test_unrelated_error_quoting_context_is_not_overflow(self):
        for message in ("invalid schema context length field", "context window field too large", "unauthorized"):
            self.assertIsNone(context_error(json.dumps({"error": {"message": message}, "input": "context length exceeded"})))

    def test_malformed_values_do_not_raise_or_invent_statistics(self):
        for value in (None, "not json", "[]", '{"error":[]}', '{"error":{"code":[],"type":{},"message":"bad"}}'):
            self.assertIsNone(context_error(value))


if __name__ == "__main__":
    unittest.main(verbosity=2)
