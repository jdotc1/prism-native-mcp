# Third-party notices

This independent integration requires GraphPad Prism, installed and licensed
separately. GraphPad names and marks belong to their owners. No Prism executable,
license, installation resource, or vendor demonstration archive is distributed.

The bundled Column seed was created by the maintainer in Prism using synthetic
observations. It retains Prism's native document and drawing serialization; the
software must use Prism to calculate and render it. Two author-metadata fields
use the maintainer's public handle. It is not a copied vendor sample dataset.

Dependencies are installed separately and remain under their upstream terms:

- MCP Python SDK — https://github.com/modelcontextprotocol/python-sdk
- Pydantic — https://github.com/pydantic/pydantic
- OpenPyXL — https://openpyxl.readthedocs.io/
- PyObjC Quartz/Vision bridges — https://github.com/ronaldoussoren/pyobjc
- Development dependencies: pytest, SciPy, PyMuPDF, and Hatchling, plus their
  transitive dependencies, as recorded in `uv.lock`.

The project license does not relicense these dependencies or GraphPad Prism.
PyMuPDF is retained in the development lock for baseline continuity; no PyMuPDF
code or binary is vendored in this export. Review upstream terms when building
or redistributing an environment.
