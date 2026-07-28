import pytest


def test_tenant_schema_is_deterministic_and_not_raw_input():
    from deeptutor.teaching.schema_names import tenant_schema_name

    schema = tenant_schema_name("org/acme")
    assert schema.startswith("tenant_")
    assert "/" not in schema
    assert schema == tenant_schema_name("org/acme")
    assert schema != tenant_schema_name("org/other")


def test_tenant_schema_normalizes_surrounding_whitespace():
    from deeptutor.teaching.schema_names import tenant_schema_name

    assert tenant_schema_name(" org/acme ") == tenant_schema_name("org/acme")


@pytest.mark.parametrize("tenant_id", ["", "   "])
def test_tenant_schema_rejects_empty_tenant_id(tenant_id):
    from deeptutor.teaching.schema_names import tenant_schema_name

    with pytest.raises(ValueError, match="^tenant_id is required$"):
        tenant_schema_name(tenant_id)
