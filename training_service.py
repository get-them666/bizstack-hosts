"""Training & onboarding content: PowerPoint decks + orientation quiz.

Two decks are built here with python-pptx:
- ``worker`` deck: how to use the phone app and website, expected behavior in hosts'
  homes, ethics, sexual harassment policy, and other new-hire orientation items.
- ``host`` deck: how BizStack Hosts works, services & pricing, the host portal, and
  funding/capital partners for leads.

Each deck is returned as in-memory ``.pptx`` bytes for storage in the document
library, and the worker quiz (10 simple questions) is defined here too.
"""

QUIZ = [
    {
        "q": "How do you get the worker app on your phone?",
        "options": [
            "Download it from the App Store",
            "Open the site on your phone and save it to your home screen",
            "It installs itself",
        ],
        "answer": 1,
    },
    {
        "q": "Why do you clock in and clock out on every job?",
        "options": [
            "So the office knows where you are",
            "Because that's how you get paid",
            "Just for fun",
        ],
        "answer": 1,
    },
    {
        "q": "When you arrive at a property, what should you do first?",
        "options": [
            "Clock in when you're inside the work area and then start cleaning",
            "Text your friends",
            "Wait outside until someone tells you to start",
        ],
        "answer": 0,
    },
    {
        "q": "Where do you find directions to your job?",
        "options": [
            "Call the office every time",
            "The map in the worker app",
            "Guess the address",
        ],
        "answer": 1,
    },
    {
        "q": "Can you eat food, use the bed, or take supplies from the host's home?",
        "options": [
            "Yes, if you're careful",
            "Only snacks",
            "No — it's the host's home; you treat everything with respect",
        ],
        "answer": 2,
    },
    {
        "q": "What should you do if you see damage or something broken at a property?",
        "options": [
            "Hide it",
            "Report it right away with a photo",
            "Fix it with tape",
        ],
        "answer": 1,
    },
    {
        "q": "How should you treat every person you meet at a property or office?",
        "options": [
            "With respect, always",
            "Only if they talk to you first",
            "Whatever, it doesn't matter",
        ],
        "answer": 0,
    },
    {
        "q": "Is unwelcome touching or sexual comments ever okay at work?",
        "options": [
            "No, never — it's harassment and won't be tolerated",
            "Yes if someone jokes about it",
            "Sometimes",
        ],
        "answer": 0,
    },
    {
        "q": "Where can you report a problem or concern safely?",
        "options": [
            "Nowhere",
            "Tell the owner or use the reporting channel — it's confidential",
            "Post it on social media",
        ],
        "answer": 1,
    },
    {
        "q": "Where do you find your paycheck and pay stubs?",
        "options": [
            "Under 'Pay' in the worker app or portal",
            "The host sends it by mail",
            "You have to ask in person",
        ],
        "answer": 0,
    },
]


BRAND = {
    "title_color": (49, 46, 129),  # indigo-900
    "accent_color": (99, 102, 241),  # indigo-500
    "dark": (15, 23, 42),  # slate-900
    "light": (248, 250, 252),  # slate-50
}


def _add_title_slide(prs, doc_title, subtitle):
    from pptx.dml.color import RGBColor
    from pptx.util import Inches, Pt

    layout = prs.slide_layouts[0]
    slide = prs.slides.add_slide(layout)
    title = slide.shapes.title
    title.text = doc_title
    title.text_frame.paragraphs[0].runs[0].font.size = Pt(40)
    title.text_frame.paragraphs[0].runs[0].font.bold = True
    title.text_frame.paragraphs[0].runs[0].font.color.rgb = RGBColor(*BRAND["title_color"])
    if slide.shapes.placeholders and len(slide.shapes.placeholders) > 1:
        sub = slide.placeholders[1]
        sub.text = subtitle
        for para in sub.text_frame.paragraphs:
            for run in para.runs:
                run.font.size = Pt(18)
                run.font.color.rgb = RGBColor(*BRAND["dark"])
    return slide


def _add_bullets_slide(prs, doc_title, bullets):
    from pptx.dml.color import RGBColor
    from pptx.util import Inches, Pt

    slide = prs.slides.add_slide(prs.slide_layouts[1])
    title = slide.shapes.title
    title.text = doc_title
    title.text_frame.paragraphs[0].runs[0].font.bold = True
    title.text_frame.paragraphs[0].runs[0].font.size = Pt(28)
    title.text_frame.paragraphs[0].runs[0].font.color.rgb = RGBColor(*BRAND["title_color"])
    body = slide.placeholders[1].text_frame
    body.clear()
    first = True
    for item in bullets:
        if first:
            para = body.paragraphs[0]
            first = False
        else:
            para = body.add_paragraph()
        para.text = item
        para.level = 0
        para.font.size = Pt(18)
        for run in para.runs:
            run.font.color.rgb = RGBColor(*BRAND["dark"])
    return slide


def _slides_content(kind: str) -> list[tuple[str, list[str]]]:
    if kind == "worker":
        return [
            ("Welcome to BizStack Hosts!", [
                "Thank you for joining our cleaning & co-hosting crew",
                "Today: your app, your job, your pay, and how we act",
                "Short quiz at the end — easy if you pay attention",
            ]),
            ("About Our Company", [
                "We clean & co-host short-term rentals (Airbnb, Vrbo, direct)",
                "Guests fund the cleaning fee — hosts pay nothing out of pocket",
                "We win by doing 5-star turnovers, every time",
            ]),
            ("Your Phone App — Download & Login", [
                "Open https://bizstackperks.com on your phone",
                "Save it to your home screen so it opens like an app",
                "Log in with your email and the PIN the owner set for you",
                "That's it — no store download needed",
            ]),
            ("Your Phone App — Jobs & Map", [
                "Your assigned jobs show with the date, time and address",
                "Tap 'Directions' / the map to navigate to the property",
                "Jobs stay on your screen so you always know your day",
            ]),
            ("Clock In / Clock Out", [
                "When you arrive, the app checks you're at the right property",
                "Clock in when you're inside the work area and start",
                "Clock out when the job is finished (before you leave!)",
                "Clock in AND out on every job — that's what pays you",
            ]),
            ("Paychecks & Pay", [
                "You get paid per job at your rate on your paycheck",
                "Find pay stubs under 'Pay' in the app or on the web portal",
                "Questions about pay? Ask the office—never negotiate in the host's home",
            ]),
            ("Using the Website Portal", [
                "The website (https://bizstackperks.com) shows the same schedule",
                "Log in at /worker-login or through /login with your email + PIN",
                "See your jobs, map, and paychecks in one place",
            ]),
            ("House Rules in Hosts' Homes", [
                "You are a guest in their property — act like it",
                "No smoking, no eating host food, no using beds or bathrooms for personal use",
                "Leave every place exactly as you found it, plus cleaner",
                "Treat the host's stuff the way you'd want yours treated",
            ]),
            ("The 5-Star Turnover Checklist", [
                "Trash out, then all linen exchanged",
                "Bathrooms + kitchen sanitized top to bottom",
                "All floors vacuumed and mopped",
                "Dust everything, restock supplies, stage the space",
                "Photo-verify each room, then mark the job done",
            ]),
            ("Staying Safe at Work", [
                "Use supplies safely; report anything unsafe",
                "Let the office know any medical issue you have before jobs",
                "If you feel unsafe at a property, leave and call the office",
            ]),
            ("Workplace Ethics & Integrity", [
                "Be honest about time, vetting, and what you complete",
                "Don't cut corners to rush — 5-star is the brand",
                "Never share host or guest information with anyone",
            ]),
            ("Sexual Harassment Policy", [
                "Zero tolerance — always, toward anyone, at work",
                "Unwelcome touching, comments, jokes, or advances are prohibited",
                "Reporting is safe and confidential — no retaliation, ever",
            ]),
            ("Reporting Problems & Concerns", [
                "Damage, maintenance, host issues → report right away with a photo",
                "Harassment or anything wrong → tell the owner or use the report channel",
                "Reporting is never punished. Ever.",
            ]),
        ]
    return [
        ("Welcome to BizStack Hosts", [
            "Short-term rental cleaning + co-hosting, powered by automation",
            "You're the kind of host we love — a property with potential",
            "This deck shows services, pricing, your portal, and growth options",
        ]),
        ("What We Do", [
            "Turnover cleaning, deep cleaning, linen restock, inspections",
            "Digital co-hosting: 24/7 AI assistant, dynamic pricing, review help",
            "Full-service management: cleaning, vendors, multi-channel bookings",
            "All backed by photo-verified quality you can see",
        ]),
        ("Service Pricing", [
            "Turnover Cleaning $120  |  Deep Cleaning $200",
            "Linen Restock $50  |  Inspection $75",
            "Cleaning is GUEST-FUNDED — you pay $0 out of pocket",
            "Co-hosting: 10-15% (digital) or 20-30% (full-service) of gross bookings",
        ]),
        ("How Booking Works", [
            "Guests contact the 24/7 assistant by text or call",
            "Assistant books the slot and sends a secure Stripe payment link",
            "Guest pays at booking — your calendar and ledger update automatically",
        ]),
        ("Your Free Rental Analysis", [
            "Get a real, data-backed earnings report for your property",
            "Home value, market rent, nightly STR estimate, self-managed vs co-hosted",
            "Free via the site — just submit your address",
        ]),
        ("Your Host Portal", [
            "Log in at /host-login to see YOUR properties, bookings and payments",
            "A live map of your portfolio",
            "Only your data — ever",
        ]),
        ("Calendar & Payments", [
            "Bookings sync to your calendar",
            "Paid bookings appear as revenue automatically",
            "Unpaid guests can be re-sent a fresh payment link any time",
        ]),
        ("Growing With Financing", [
            "Need capital to furnish, stage, or convert your property?",
            "BizStack connects eligible leads with bank/financing partners",
            "Funding is handled directly with the partner bank — we just connect you",
        ]),
        ("What Makes a 5-Star Host", [
            "Fast, friendly guest communication",
            "Immaculate turnover between guests (our job!)",
            "Clear house rules and a clean, stocked property",
        ]),
        ("Support & Next Steps", [
            "Questions? Text/call +1 (757) 846-9275 or email hello@bizstackperks.com",
            "Submit your property for a free analysis",
            "Let's get your listing earning",
        ]),
    ]


def deck_slides(kind: str) -> list[dict]:
    """Deck slides as {title, bullets, voice} — voice is the bot's narration text."""
    parsed = "host" if kind != "worker" else "worker"
    phrases = {
        "Welcome to BizStack Hosts!": "Welcome to BizStack Hosts.",
        "Welcome to BizStack Hosts": "Welcome to BizStack Hosts.",
        "About Our Company": "About our company.",
    }
    out = []
    for title, bullets in _slides_content(parsed):
        voice = phrases.get(title, f"Next up — {title}. ") + " "
        voice += " ".join(b for b in bullets if b)
        out.append({"title": title, "bullets": bullets, "voice": voice})
    return out


def build_deck(kind: str) -> bytes:
    """Build a powerpoint deck. kind: 'worker' or 'host'. Returns .pptx bytes."""
    from pptx import Presentation

    kind = "host" if kind != "worker" else "worker"
    prs = Presentation()
    prs.slide_width = 12192000
    prs.slide_height = 6858000

    if kind == "worker":
        doc_title = "New Worker Orientation"
        subtitle = "Your app, your job, your pay, and how we do it — plus your quick quiz."
    else:
        doc_title = "Host & Lead Onboarding"
        subtitle = "Welcome to BizStack Hosts — how we help your property earn."

    _add_title_slide(prs, doc_title, subtitle)
    for title, bullets in _slides_content(kind):
        _add_bullets_slide(prs, title, bullets)

    from io import BytesIO
    buf = BytesIO()
    prs.save(buf)
    return buf.getvalue()


def grade_quiz(answers: list[int]) -> dict:
    """Grade quiz answers (list of 10 option indexes) -> score/percent + right/wrong."""
    if len(answers) != len(QUIZ):
        raise ValueError("expected 10 answers")
    correct = [i for i, a in enumerate(answers) if a == QUIZ[i]["answer"]]
    pct = round(100 * len(correct) / len(QUIZ))
    passed = pct >= 70
    return {
        "total": len(QUIZ),
        "correct": len(correct),
        "percent": pct,
        "passed": passed,
    }