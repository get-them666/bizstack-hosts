"""Prefab document definitions for the Broom Service documents & forms library.

Each definition is data-driven: a title, category, fillable fields grouped
into sections, optional agreement clauses, and signature blocks. The same
definition is rendered to PDF (fpdf2) and DOCX (python-docx) by
documents_service.py, and drives the on-screen filler form.

These are starter templates only. They are NOT legal advice; every form
should be reviewed by an attorney before it is used for a real transaction.
"""

REALTY_FORMS = [
    {
        "key": "lease",
        "title": "Residential Lease Agreement",
        "category": "Realty",
        "blurb": "Short-term residential rental agreement between owner and guest/tenant. Add your jurisdiction clauses.",
        "sections": [
            {
                "heading": "1. Parties",
                "fields": [
                    {"key": "landlord", "label": "Landlord / Owner full name & entity", "type": "text", "required": True},
                    {"key": "tenant", "label": "Tenant / Guest full name", "type": "text", "required": True},
                    {"key": "property", "label": "Property address", "type": "text", "required": True},
                    {"key": "date", "label": "Agreement date", "type": "date", "required": True},
                ],
            },
            {
                "heading": "2. Term & Rent",
                "fields": [
                    {"key": "start_date", "label": "Start date", "type": "date", "required": True},
                    {"key": "end_date", "label": "End date", "type": "date", "required": True},
                    {"key": "rent", "label": "Rent amount (e.g. $1,850.00 /mo)", "type": "text", "required": True},
                    {"key": "security_deposit", "label": "Security deposit", "type": "text"},
                    {"key": "occupancy", "label": "Max occupants", "type": "number"},
                    {"key": "pets", "label": "Pets policy", "type": "textarea"},
                ],
            },
            {
                "heading": "3. House Rules & Use",
                "fields": [
                    {"key": "use", "label": "Authorized use (e.g. short-term vacation rental)", "type": "textarea"},
                    {"key": "smoking", "label": "Smoking policy", "type": "textarea"},
                    {"key": "noise", "label": "Noise / quiet-hours policy", "type": "textarea"},
                    {"key": "extra_fees", "label": "Additional fees (cleaning, damage, late)", "type": "textarea"},
                ],
            },
        ],
        "agreements": [
            "The tenant agrees to keep the property in clean, tenantable condition and to immediately report any damage or needed repairs to the landlord.",
            "The tenant shall not assign this agreement or sublet the property without the landlord's prior written consent.",
            "Either party must give written notice before terminating this agreement in accordance with applicable law.",
            "This agreement is governed by the laws of the state where the property is located.",
        ],
        "signatories": ["Landlord", "Tenant"],
        "disclaimer": "Starter template — have an attorney review for your state/municipality before use.",
    },
    {
        "key": "lease_turnover",
        "title": "Turnover Cleaning Services Agreement",
        "category": "Realty",
        "blurb": "Bundled cleaning-fee addendum/agreement for guests booking a property with the guest-funded turnover cleaning program.",
        "sections": [
            {
                "heading": "1. Parties & Property",
                "fields": [
                    {"key": "owner", "label": "Owner / Operator", "type": "text", "required": True},
                    {"key": "guest", "label": "Guest full name", "type": "text", "required": True},
                    {"key": "property", "label": "Property address", "type": "text", "required": True},
                    {"key": "stay", "label": "Stay dates", "type": "text", "required": True},
                ],
            },
            {
                "heading": "2. Cleaning Fee & Scope",
                "fields": [
                    {"key": "fee", "label": "Turnover cleaning fee", "type": "text", "required": True},
                    {"key": "scope", "label": "Cleaning scope (rooms, linen, staging)", "type": "textarea"},
                    {"key": "collected", "label": "Collected via (platform line-item / Stripe)", "type": "text"},
                ],
            },
        ],
        "agreements": [
            "The guest authorizes the turnover cleaning fee at booking. Funds collected above are used exclusively for professional turnover cleaning of the property.",
            "Cleaning is performed between departures by verified, insured cleaners.",
        ],
        "signatories": ["Owner / Operator", "Guest"],
        "disclaimer": "Starter template — review with counsel before use.",
    },
    {
        "key": "property_mgmt",
        "title": "Property Management Agreement (STR)",
        "category": "Realty",
        "blurb": "Agreement between owner and manager for co-hosting / full-service short-term rental management.",
        "sections": [
            {
                "heading": "1. Engagement",
                "fields": [
                    {"key": "owner", "label": "Owner", "type": "text", "required": True},
                    {"key": "manager", "label": "Manager (Broom Service)", "type": "text", "required": True},
                    {"key": "property", "label": "Property address", "type": "text", "required": True},
                    {"key": "begin", "label": "Effective date", "type": "date", "required": True},
                ],
            },
            {
                "heading": "2. Services & Compensation",
                "fields": [
                    {"key": "services", "label": "Scope of management services", "type": "textarea"},
                    {"key": "tier", "label": "Management tier (Digital Co-Hosting / Full-Service)", "type": "text"},
                    {"key": "fee", "label": "Management fee (% of gross nightly revenue)", "type": "text", "required": True},
                    {"key": "term_months", "label": "Initial term (months)", "type": "number"},
                    {"key": "renewal", "label": "Renewal / notice terms", "type": "textarea"},
                ],
            },
        ],
        "agreements": [
            "The manager shall operate the listing, coordinate guest communications, and manage turnover logistics.",
            "Owner retains ownership of the property and platform accounts; access is limited to the scope of this agreement.",
            "Either party may terminate per the notice terms stated herein.",
        ],
        "signatories": ["Owner", "Manager"],
        "disclaimer": "Starter template — review with counsel before use.",
    },
]

FINANCE_FORMS = [
    {
        "key": "invoice",
        "title": "Invoice / Statement",
        "category": "Finance",
        "blurb": "Itemized invoice for services rendered (cleaning, management, co-hosting).",
        "sections": [
            {
                "heading": "1. Billing",
                "fields": [
                    {"key": "invoice_no", "label": "Invoice number", "type": "text", "required": True},
                    {"key": "bill_to", "label": "Bill to (client / guest)", "type": "text", "required": True},
                    {"key": "bill_date", "label": "Invoice date", "type": "date", "required": True},
                    {"key": "due_date", "label": "Payment due date", "type": "date"},
                ],
            },
            {
                "heading": "2. Line Items",
                "fields": [
                    {"key": "item1", "label": "Line item 1", "type": "text"},
                    {"key": "amount1", "label": "Amount 1", "type": "text"},
                    {"key": "item2", "label": "Line item 2", "type": "text"},
                    {"key": "amount2", "label": "Amount 2", "type": "text"},
                    {"key": "discount", "label": "Discount / adjustments", "type": "text"},
                    {"key": "notes", "label": "Payment instructions / notes", "type": "textarea"},
                ],
            },
        ],
        "agreements": ["Payment is due on the due date shown. Late payments may incur fees at the rate stated by law or in this statement."],
        "signatories": ["Authorized Representative"],
        "disclaimer": "Starter template — verify tax requirements with your accountant.",
    },
    {
        "key": "promissory_note",
        "title": "Promissory Note",
        "category": "Finance",
        "blurb": "Promissory note for loaned funds with repayment schedule.",
        "sections": [
            {
                "heading": "1. Note Terms",
                "fields": [
                    {"key": "borrower", "label": "Borrower", "type": "text", "required": True},
                    {"key": "lender", "label": "Lender", "type": "text", "required": True},
                    {"key": "principal", "label": "Principal amount", "type": "text", "required": True},
                    {"key": "date", "label": "Note date", "type": "date", "required": True},
                    {"key": "interest_rate", "label": "Interest rate (APR %)", "type": "text"},
                    {"key": "due_date", "label": "Maturity date", "type": "date"},
                    {"key": "schedule", "label": "Repayment schedule", "type": "textarea"},
                ],
            },
        ],
        "agreements": [
            "For value received, the borrower promises to pay the lender the principal sum plus interest per the schedule above.",
            "Default on any payment may accelerate the remaining balance due immediately.",
            "This note is governed by applicable state law.",
        ],
        "signatories": ["Borrower"],
        "disclaimer": "Starter template — consult counsel, and confirm usury/state limits on interest.",
    },
    {
        "key": "receipt",
        "title": "Payment Receipt",
        "category": "Finance",
        "blurb": "Acknowledgment of a payment received (Stripe checkout, cash, wire).",
        "sections": [
            {
                "heading": "1. Payment Details",
                "fields": [
                    {"key": "received_from", "label": "Received from", "type": "text", "required": True},
                    {"key": "amount", "label": "Amount received", "type": "text", "required": True},
                    {"key": "for", "label": "Payment for", "type": "textarea", "required": True},
                    {"key": "method", "label": "Payment method (Stripe / cash / wire)", "type": "text"},
                    {"key": "date", "label": "Received date", "type": "date", "required": True},
                ],
            },
        ],
        "agreements": ["This receipt acknowledges receipt of the amount above. Balance, if any, is shown on the associated statement."],
        "signatories": ["Authorized Representative"],
        "disclaimer": "Starter template.",
    },
]

LEGAL_FORMS = [
    {
        "key": "contractor",
        "title": "Independent Contractor Agreement (Cleaning)",
        "category": "Legal",
        "blurb": "Agreement between Broom Service and a cleaning contractor.",
        "sections": [
            {
                "heading": "1. Engagement",
                "fields": [
                    {"key": "contractor", "label": "Contractor name", "type": "text", "required": True},
                    {"key": "company", "label": "Company (Broom Service)", "type": "text", "required": True},
                    {"key": "date", "label": "Agreement date", "type": "date", "required": True},
                    {"key": "services", "label": "Services (turnover cleaning, staging, linen)", "type": "textarea", "required": True},
                    {"key": "rate", "label": "Pay rate / job", "type": "text", "required": True},
                ],
            },
            {
                "heading": "2. Independent Contractor Status",
                "fields": [
                    {"key": "provide_equipment", "label": "Contractor supplies (supplies, equipment)", "type": "textarea"},
                    {"key": "supervision", "label": "Supervision / work orders arrangements", "type": "textarea"},
                    {"key": "term", "label": "Term & termination", "type": "textarea"},
                ],
            },
        ],
        "agreements": [
            "The contractor is an independent contractor, not an employee, and is responsible for their own taxes, insurance, and workers' compensation.",
            "The contractor controls the method and means of performing the services, subject to the outcome standards in this agreement.",
            "The contractor shall keep confidential all proprietary business information of the company.",
        ],
        "signatories": ["Contractor", "Company"],
        "disclaimer": "Starter template — independent-contractor classifications are highly regulated; have it reviewed before engaging workers.",
    },
    {
        "key": "nda",
        "title": "Mutual Non-Disclosure Agreement",
        "category": "Legal",
        "blurb": "NDA for sharing confidential business, pricing, and listing information.",
        "sections": [
            {
                "heading": "1. Parties & Purpose",
                "fields": [
                    {"key": "party_a", "label": "Party A (Discloser)", "type": "text", "required": True},
                    {"key": "party_b", "label": "Party B (Recipient)", "type": "text", "required": True},
                    {"key": "date", "label": "Effective date", "type": "date", "required": True},
                    {"key": "purpose", "label": "Purpose of disclosure", "type": "textarea", "required": True},
                    {"key": "term", "label": "Confidentiality term (years)", "type": "number"},
                ],
            },
        ],
        "agreements": [
            "Recipient shall not use confidential information except to evaluate the purpose stated above.",
            "Recipient shall not disclose confidential information to any third party without prior written consent.",
            "Confidential information excludes information already public or independently developed.",
        ],
        "signatories": ["Party A", "Party B"],
        "disclaimer": "Starter template.",
    },
]

ALL_FORMS = REALTY_FORMS + FINANCE_FORMS + LEGAL_FORMS
FORMS_BY_KEY = {f["key"]: f for f in ALL_FORMS}

CATEGORY_LABELS = ["Realty", "Finance", "Legal"]

def all_fields(form):
    return [f for sec in form.get("sections", []) for f in sec.get("fields", [])]

def form_categories():
    return {
        "Realty": REALTY_FORMS,
        "Finance": FINANCE_FORMS,
        "Legal": LEGAL_FORMS,
    }