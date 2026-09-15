"""The Design harness: a designer with a shared document.

The model and the user hold opposite ends of the same artboard HTML. The
harness is the authority on that document: every change — the model's edit
tools, the page's inspector, a drag in the iframe — lands as an op from one
small vocabulary, applied to the harness's own server-side copy, checkpointed
as a version, and pushed to the page. The iframe is a view; the harness's
copy is the document.

`state.py` is the session's shape; `dom.py` is the server-side document the
op vocabulary mutates; `session.py` is the wrapper the tools and the page
both write through; `mutate.py` is the page's door.
"""