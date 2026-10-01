from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, Field, ConfigDict


BBox = tuple[float, float, float, float]


class WordToken(BaseModel):
    text: str
    page: int
    bbox: BBox
    confidence: float = 1.0
    source: Literal["native_pdf", "ocr", "azure", "structured_file"] = "ocr"


class PageResult(BaseModel):
    page: int
    width: float
    height: float
    source_type: Literal["native_pdf", "scan_ocr", "image_ocr", "azure", "structured_file"]
    text: str
    words: list[WordToken] = Field(default_factory=list)
    confidence: float = 1.0
    rotation_applied: float = 0.0
    ocr_quality: float = 0.0
    image_state: str | None = None
    image_state_confidence: float = 0.0
    selected_ocr_action: str | None = None
    ocr_diagnostics: dict[str, Any] = Field(default_factory=dict)
    image_quality_metrics: dict[str, float] = Field(default_factory=dict)
    vector_lines: list[dict[str, Any]] = Field(default_factory=list)
    vector_rectangles: list[BBox] = Field(default_factory=list)


class Evidence(BaseModel):
    page: int | None = None
    bbox: BBox | None = None
    source_text: str | None = None
    extraction_method: str | None = None


class ExtractedField(BaseModel):
    value: Any = None
    raw_value: str | None = None
    normalized_value: Any = None
    ocr_confidence: float = 0.0
    semantic_confidence: float = 0.0
    validation_confidence: float = 1.0
    final_confidence: float = 0.0
    validation_status: Literal["VALID", "WARNING", "INVALID", "NOT_CHECKED"] = "NOT_CHECKED"
    warnings: list[str] = Field(default_factory=list)
    evidence: Evidence = Field(default_factory=Evidence)


class Address(BaseModel):
    # Raw/source representation
    raw: str | None = None
    raw_lines: list[str] = Field(default_factory=list)
    line1: str | None = None
    line2: str | None = None
    line3: str | None = None

    # Postal components
    building: str | None = None
    building_number: str | None = None
    residence: str | None = None
    entrance: str | None = None
    floor: str | None = None
    unit: str | None = None
    house_number: str | None = None
    house_number_suffix: str | None = None
    street_type: str | None = None
    street_name: str | None = None
    street: str | None = None
    lieu_dit: str | None = None
    industrial_zone: str | None = None
    business_park: str | None = None
    address_complement: str | None = None
    po_box: str | None = None
    tsa: str | None = None
    cs: str | None = None
    postal_routing_code: str | None = None
    postal_code: str | None = None
    city: str | None = None
    cedex: bool | None = None
    cedex_number: str | None = None
    district: str | None = None
    insee_code: str | None = None
    state: str | None = None
    region: str | None = None
    country: str | None = None
    country_code: str | None = None

    # Optional authoritative validation/geocoding
    latitude: float | None = None
    longitude: float | None = None
    canonical_label: str | None = None
    verification_status: str | None = None
    verification_provider: str | None = None
    verification_score: float | None = None
    verification_type: str | None = None


class Contact(BaseModel):
    name: str | None = None
    firstname: str | None = None
    lastname: str | None = None
    department: str | None = None
    email: str | None = None
    phone: str | None = None
    fax: str | None = None


class Party(BaseModel):
    id: str | None = None
    code: str | None = None
    sap_id: str | None = None
    name: str | None = None
    legal_name: str | None = None
    trading_name: str | None = None
    department: str | None = None
    division: str | None = None
    business_unit: str | None = None
    address: Address = Field(default_factory=Address)
    contact: Contact = Field(default_factory=Contact)
    vat_number: str | None = None
    tax_number: str | None = None
    company_registration_number: str | None = None
    duns: str | None = None
    gln: str | None = None
    phone: str | None = None
    fax: str | None = None
    email: str | None = None
    website: str | None = None


class BusinessAddress(BaseModel):
    address_id: str
    role: str
    role_label: str
    party_name: str | None = None
    party_code: str | None = None
    department: str | None = None
    contact_name: str | None = None
    contact_email: str | None = None
    contact_phone: str | None = None
    address: Address = Field(default_factory=Address)
    formatted_address: str | None = None
    address_fingerprint: str | None = None
    role_confidence: float = 0.0
    address_confidence: float = 0.0
    confidence: float = 0.0
    component_confidence: dict[str, float] = Field(default_factory=dict)
    validation_status: str = "UNVERIFIED"
    validation_score: float = 0.0
    is_verified_real_address: bool = False
    evidence: Evidence = Field(default_factory=Evidence)
    shared_with_roles: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class ScheduleLine(BaseModel):
    quantity: float | None = None
    delivery_date: str | None = None
    confidence: float = 0.0


class AdditionalCharge(BaseModel):
    charge_type: str = "other"
    code: str | None = None
    description: str | None = None
    parent_line_number: str | None = None
    parent_material_number: str | None = None
    supplier_reference: str | None = None
    quantity: float | None = None
    uom: str | None = None
    unit_price: float | None = None
    amount: float | None = None
    currency: str | None = None
    page: int | None = None
    confidence: float = 0.0
    evidence: Evidence = Field(default_factory=Evidence)
    warnings: list[str] = Field(default_factory=list)


class LineItem(BaseModel):
    line_number: str | None = None
    subline_number: str | None = None
    parent_line_number: str | None = None
    material_number: str | None = None
    article_number: str | None = None
    product_code: str | None = None
    customer_material_number: str | None = None
    supplier_material_number: str | None = None
    manufacturer_part_number: str | None = None
    manufacturer_name: str | None = None
    ean: str | None = None
    gtin: str | None = None
    upc: str | None = None
    sku: str | None = None
    description: str | None = None
    short_description: str | None = None
    long_description: str | None = None
    category: str | None = None
    product_family: str | None = None
    quantity: float | None = None
    ordered_quantity: float | None = None
    confirmed_quantity: float | None = None
    delivered_quantity: float | None = None
    uom: str | None = None
    alternative_uom: str | None = None
    pack_quantity: float | None = None
    pack_size: str | None = None
    unit_price: float | None = None
    gross_unit_price: float | None = None
    net_unit_price: float | None = None
    price_unit: float | None = None
    currency: str | None = None
    discount_percent: float | None = None
    discount_amount: float | None = None
    surcharge_percent: float | None = None
    surcharge_amount: float | None = None
    line_net_amount: float | None = None
    line_gross_amount: float | None = None
    line_total: float | None = None
    tax_code: str | None = None
    tax_rate: float | None = None
    tax_amount: float | None = None
    vat_code: str | None = None
    vat_rate: float | None = None
    vat_amount: float | None = None
    requested_delivery_date: str | None = None
    confirmed_delivery_date: str | None = None
    delivery_week: str | None = None
    delivery_period: str | None = None
    shipping_date: str | None = None
    plant: str | None = None
    warehouse: str | None = None
    storage_location: str | None = None
    incoterm: str | None = None
    country_of_origin: str | None = None
    customs_code: str | None = None
    hs_code: str | None = None
    net_weight: float | None = None
    gross_weight: float | None = None
    weight_unit: str | None = None
    cost_center: str | None = None
    profit_center: str | None = None
    gl_account: str | None = None
    wbs_element: str | None = None
    project_number: str | None = None
    internal_order: str | None = None
    contract_number: str | None = None
    contract_line: str | None = None
    quote_number: str | None = None
    quote_date: str | None = None
    quote_line: str | None = None
    customer_reference: str | None = None
    requisition_number: str | None = None
    requisition_line: str | None = None
    schedule_lines: list[ScheduleLine] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    raw_text: str | None = None
    page: int | None = None
    bbox: BBox | None = None
    confidence: float = 0.0
    validation_status: str = "NOT_CHECKED"
    warnings: list[str] = Field(default_factory=list)


class Totals(BaseModel):
    subtotal: float | None = None
    calculated_line_total: float | None = None
    calculated_line_total_confidence: float | None = None
    calculated_additional_charges: float | None = None
    calculated_document_net: float | None = None
    total_product_lines: float | None = None
    total_line_charges: float | None = None
    total_line_taxes: float | None = None
    total_admin_fees: float | None = None
    total_packaging: float | None = None
    total_other_fees: float | None = None
    total_weight: float | None = None
    weight_unit: str | None = None
    total_net: float | None = None
    total_gross: float | None = None
    total_discount: float | None = None
    total_surcharge: float | None = None
    total_freight: float | None = None
    total_shipping: float | None = None
    total_tax: float | None = None
    total_vat: float | None = None
    total_before_tax: float | None = None
    total_after_tax: float | None = None
    rounding: float | None = None
    deposit: float | None = None
    amount_due: float | None = None
    grand_total: float | None = None
    currency: str | None = None


class TaxSummary(BaseModel):
    type: str | None = None
    rate: float | None = None
    taxable_amount: float | None = None
    tax_amount: float | None = None


class ExtraField(BaseModel):
    label: str
    value: str
    page: int | None = None
    bbox: BBox | None = None
    confidence: float = 0.0


class GenericTable(BaseModel):
    page: int
    headers: list[str] = Field(default_factory=list)
    rows: list[list[str]] = Field(default_factory=list)
    confidence: float = 0.0


class DocumentMeta(BaseModel):
    id: str
    filename: str
    mime_type: str | None = None
    extension: str
    file_size: int
    sha256: str
    page_count: int
    document_type: str = "PURCHASE_ORDER"
    document_subtype: str | None = None
    is_pdf: bool = False
    is_image: bool = False
    is_scan: bool = False
    is_native_pdf: bool = False
    is_mixed_pdf: bool = False
    detected_languages: list[str] = Field(default_factory=list)
    primary_language: str | None = None
    has_text_layer: bool = False
    has_images: bool = False
    has_tables: bool = False
    has_signature: bool = False
    has_stamp: bool = False
    has_barcode: bool = False
    has_qrcode: bool = False
    encrypted: bool = False
    password_protected: bool = False
    ocr_required: bool = False
    ocr_engine: str | None = None
    processing_timestamp: str
    processing_duration_seconds: float = 0.0
    model_version: str = "0.1.0"


class PurchaseOrderHeader(BaseModel):
    number: ExtractedField = Field(default_factory=ExtractedField)
    external_number: ExtractedField = Field(default_factory=ExtractedField)
    internal_number: ExtractedField = Field(default_factory=ExtractedField)
    type: ExtractedField = Field(default_factory=ExtractedField)
    subtype: ExtractedField = Field(default_factory=ExtractedField)
    version: ExtractedField = Field(default_factory=ExtractedField)
    revision: ExtractedField = Field(default_factory=ExtractedField)
    amendment_number: ExtractedField = Field(default_factory=ExtractedField)
    status: ExtractedField = Field(default_factory=ExtractedField)
    order_date: ExtractedField = Field(default_factory=ExtractedField)
    creation_date: ExtractedField = Field(default_factory=ExtractedField)
    issue_date: ExtractedField = Field(default_factory=ExtractedField)
    valid_from: ExtractedField = Field(default_factory=ExtractedField)
    valid_until: ExtractedField = Field(default_factory=ExtractedField)
    required_date: ExtractedField = Field(default_factory=ExtractedField)
    expected_delivery_date: ExtractedField = Field(default_factory=ExtractedField)
    priority: ExtractedField = Field(default_factory=ExtractedField)
    language: ExtractedField = Field(default_factory=ExtractedField)
    currency: ExtractedField = Field(default_factory=ExtractedField)
    description: ExtractedField = Field(default_factory=ExtractedField)
    subject: ExtractedField = Field(default_factory=ExtractedField)
    customer_reference: ExtractedField = Field(default_factory=ExtractedField)
    vendor_reference: ExtractedField = Field(default_factory=ExtractedField)
    contract_number: ExtractedField = Field(default_factory=ExtractedField)
    framework_contract_number: ExtractedField = Field(default_factory=ExtractedField)
    quote_number: ExtractedField = Field(default_factory=ExtractedField)
    rfq_number: ExtractedField = Field(default_factory=ExtractedField)
    project_number: ExtractedField = Field(default_factory=ExtractedField)
    project_name: ExtractedField = Field(default_factory=ExtractedField)
    sales_order_reference: ExtractedField = Field(default_factory=ExtractedField)
    requisition_number: ExtractedField = Field(default_factory=ExtractedField)
    account_number: ExtractedField = Field(default_factory=ExtractedField)


class CommercialTerms(BaseModel):
    payment_terms: str | None = None
    payment_terms_code: str | None = None
    payment_due_days: int | None = None
    payment_due_date: str | None = None
    payment_method: str | None = None
    discount_percent: float | None = None
    discount_amount: float | None = None
    early_payment_discount: float | None = None
    early_payment_days: int | None = None
    freight_terms: str | None = None
    freight_amount: float | None = None
    shipping_cost: float | None = None
    handling_cost: float | None = None
    packaging_cost: float | None = None
    insurance_cost: float | None = None
    surcharge_amount: float | None = None
    surcharge_percent: float | None = None
    minimum_order_value: float | None = None
    price_validity_date: str | None = None
    price_basis: str | None = None


class Logistics(BaseModel):
    incoterm: str | None = None
    incoterm_location: str | None = None
    delivery_terms: str | None = None
    shipping_terms: str | None = None
    transport_mode: str | None = None
    carrier: str | None = None
    carrier_code: str | None = None
    shipping_method: str | None = None
    route: str | None = None
    delivery_location: str | None = None
    requested_delivery_date: str | None = None
    confirmed_delivery_date: str | None = None
    delivery_week: str | None = None
    delivery_period: str | None = None
    requested_shipping_date: str | None = None
    delivery_time: str | None = None
    partial_delivery_allowed: bool | None = None
    backorder_allowed: bool | None = None
    expedited_shipping: bool | None = None
    freight_account: str | None = None
    tracking_reference: str | None = None


class ValidationSummary(BaseModel):
    status: Literal["PASS", "WARNING", "FAIL"] = "PASS"
    checks: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class PurchaseOrderResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    document: DocumentMeta
    purchase_order: PurchaseOrderHeader = Field(default_factory=PurchaseOrderHeader)
    buyer: Party = Field(default_factory=Party)
    supplier: Party = Field(default_factory=Party)
    sold_to: Party = Field(default_factory=Party)
    bill_to: Party = Field(default_factory=Party)
    ship_to: Party = Field(default_factory=Party)
    deliver_to: Party = Field(default_factory=Party)
    invoice_to: Party = Field(default_factory=Party)
    payer: Party = Field(default_factory=Party)
    end_customer: Party = Field(default_factory=Party)
    consignee: Party = Field(default_factory=Party)
    ship_from: Party = Field(default_factory=Party)
    business_addresses: list[BusinessAddress] = Field(default_factory=list)
    commercial: CommercialTerms = Field(default_factory=CommercialTerms)
    logistics: Logistics = Field(default_factory=Logistics)
    lines: list[LineItem] = Field(default_factory=list)
    additional_charges: list[AdditionalCharge] = Field(default_factory=list)
    totals: Totals = Field(default_factory=Totals)
    taxes: list[TaxSummary] = Field(default_factory=list)
    tax_identifiers: list[dict[str, Any]] = Field(default_factory=list)
    notes: dict[str, list[str]] = Field(default_factory=dict)
    extra_fields: list[ExtraField] = Field(default_factory=list)
    unclassified_tables: list[GenericTable] = Field(default_factory=list)
    pages: list[PageResult] = Field(default_factory=list)
    validation: ValidationSummary = Field(default_factory=ValidationSummary)
    overall_confidence: float = 0.0
    requires_human_review: bool = True
