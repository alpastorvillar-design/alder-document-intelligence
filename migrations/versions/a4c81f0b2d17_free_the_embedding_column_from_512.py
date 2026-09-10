"""Let the embedding column hold whatever a provider returns

The column was `vector(512)`, which was the hashing baseline's width and
nothing else's. That made every learned model unusable without a migration:
`bge-m3` is 1024, `qwen3-embedding` is 2560, `text-embedding-3-small` is 1536
natively. The fixed width was documenting one provider as if it were the
schema.

pgvector accepts a `vector` column with no dimension modifier: rows may then
carry different widths, and `vector_dims()` reports each. Cosine distance
still requires both operands to have the same width, and the existing
`embedding_config_hash` filter already guarantees that - a query only ever
compares against rows written by the same provider, model and dimension.

What is given up is indexability: HNSW and IVFFlat need a fixed dimension.
Nothing is lost today, because this project does exact search on a
sequential scan by deliberate decision - see docs/rag.md - and an approximate
index should only arrive with a benchmark that shows it is needed. When it
does, the column can be narrowed back to whatever that deployment's single
provider returns.

Existing rows keep their values: widening the type does not rewrite them, and
their `embedding_config_hash` keeps them separate from anything new.

Revision ID: a4c81f0b2d17
Revises: d71e9e9f4c2a
"""

from __future__ import annotations

from alembic import op

revision = "a4c81f0b2d17"
down_revision = "d71e9e9f4c2a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # `USING embedding::vector` is not needed: dropping the dimension
    # modifier is a widening of the same type, so the cast is implicit and
    # the existing 512-wide rows are untouched.
    op.execute("ALTER TABLE document_chunks ALTER COLUMN embedding TYPE public.vector")


def downgrade() -> None:
    # Narrowing back would fail on any row wider than 512, so those rows are
    # cleared first. They are a cache: `iep reindex` rebuilds them, and the
    # config hash means nothing else depends on them being present.
    op.execute(
        "UPDATE document_chunks SET embedding = NULL, embedding_provider = NULL, "
        "embedding_model = NULL, embedding_config_hash = NULL "
        # Schema-qualified: the extension lives in `public`, and a migration
        # must not depend on whatever `search_path` happens to be.
        "WHERE embedding IS NOT NULL AND public.vector_dims(embedding) <> 512"
    )
    op.execute("ALTER TABLE document_chunks ALTER COLUMN embedding TYPE public.vector(512)")
