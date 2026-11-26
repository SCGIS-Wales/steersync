---
inclusion: fileMatch
fileMatchPattern: ["src/api/**/*.py", "tests/api/**/*.py"]
metadata:
  owner: "platform/api-guild"
  tags: "api,security"
  references: "#[[file:docs/examples/reference/adr-0007-errors.md]] https://example.org/api-style"
  review-by: "2027-03-31"
  status: "active"
---

# API conventions

Every HTTP handler in this service follows these rules so clients see one consistent API.

## Rules

- Return the error envelope for every non 2xx response, so clients parse one shape.
- Paginate list endpoints with an opaque cursor, so pages stay stable under inserts.
- Never log request bodies, because they can contain personal data.

## Examples

Do:

```python
return error_envelope(status=404, code="order-not-found")
```

Avoid:

```python
return {"error": "not found"}, 404
```

## References

- Error envelope: #[[file:docs/examples/reference/errors.md:12-40]]
