"""WebForms DOM automation — Playwright interactions for ASP.NET WebForms pages.

All interactions go through BehaviorEngine. No raw page.click() calls.
DOM selectors HAR-validated from 3 recorded Carelon submissions (3,956 entries).

Every method returns a result dict: {"ok": bool, "message": str|None, "data": {}}.
Portal messages are captured on every step — this is how intelligence grows.

ARCHITECTURE NOTE: The Carelon portal is a classic ASP.NET WebForms app.
- Single URL: https://www.providerportal.com/Default.aspx
- Navigation via full-page postbacks with __VIEWSTATE + __EVENTTARGET
- Clinical questions handled by a separate ClinicalFacade.aspx JSON SPA
"""
from __future__ import annotations

import logging
import re
from datetime import date

from app.portal.behavior_engine import BehaviorEngine
from app.portal.page_reader import PageReader
from app.portal.session import PlaywrightPortalSession

logger = logging.getLogger(__name__)

# HAR-validated selectors — mapped from 3 recorded submissions
SEL = {
    # --- Member Search (SOP Step 1) ---
    "dos": "#asPrimary_ctl00_txtDateOfService",
    "first_name": "#asPrimary_ctl00_TxbFirstName",
    "last_name": "#asPrimary_ctl00_TxbLastName",
    "member_number": "#asPrimary_ctl00_TxbMemberNumber",
    "dob": "#asPrimary_ctl00_TxbDateOfBirth",
    "find_member": "#asPrimary_ctl00_BtnSearch",

    # --- Start Order (SOP Step 2) ---
    "selected_type_hidden": "#asPrimary_ctl00_hdnSelectedType",
    "phone": "#txbPhone",
    "start_order": "#cmdContinue",

    # --- Existing Auths (SOP Step 3) ---
    "next_button": "#asPrimary_ctl00_cmdNext",

    # --- Provider Search (SOP Step 4) ---
    "prov_search_type_tin_npi": "#asSearch_ctl00_rbSearchType_1",
    "prov_npi_radio": "#asSearch_ctl00_rbTINorNPIOption_1",
    "prov_npi_field": "#asSearch_ctl00_tbTINNPI",
    "prov_state": "#asSearch_ctl00_ddlState",
    "prov_search_btn": "#asSearch_ctl00_btnSearch",
    "prov_fax": "#asPrimary_ctl00_txbFax",

    # --- Facility Search (SOP Step 8) ---
    "fac_advanced_link": "[id*='lbProviderSearchAdvanced']",
    "fac_name": "#asSearch_ctl00_tbFacilityName",
    "fac_npi": "#asSearch_ctl00_tbNPI",
    "fac_state": "#asSearch_ctl00_ddlState",
    "fac_zip": "#asSearch_ctl00_tbZip",
    "fac_find_btn": "#asSearch_ctl00_btnSearch",
    "fac_continue": "#asPrimary_ctl00_btnContinue",

    # --- Submit (SOP Step 9) ---
    "submit_request": "#asPrimary_ctl00_cmdSubmitRequest",
    "comments": "#asPrimary_ctl00_txtComments",

    # --- Confirmation (extract) ---
    "conf_order_id": "#asPrimary_ctl00_lblRequestNumberSumm",
    "conf_status": "#asPrimary_ctl00_lblReqStatSumm",
    "conf_valid_from": "#asPrimary_ctl00_lblValidDateFromSumm",
    "conf_valid_through": "#asPrimary_ctl00_lblValidDateToSumm",
    "conf_health_plan": "#asPrimary_ctl00_lblHealthPl1Summ",

    # --- Terms ---
    "agree_button": "#asPrimary_ctl00_cmdAgreeContinue",
}


# Blocking modals / error layouts we expect to see AFTER clicking Submit.
# Evaluated top-down; first match wins. `title_regex` is matched against the
# title of any visible dialog/modal; `body_regex` against the visible body
# text of the dialog OR the whole page. `cancel_button_text` is the exact
# text of the button we click to back out of the modal cleanly (None → no
# cancel attempt, fall back to homepage navigation).
SUBMISSION_ERROR_PATTERNS: list[dict] = [
    {
        "error_type": "duplicate",
        "title_regex": r"duplicate order review",
        "body_regex":  r"duplicate of a previous request",
        "cancel_button_text": "Cancel Request",
    },
    {
        "error_type": "criteria_not_met",
        "title_regex": r"(?:exam summary|clinical criteria)",
        "body_regex":  r"does not meet medical necessity|criteria not met",
        "cancel_button_text": "Withdraw this Request",
    },
    {
        "error_type": "portal_error",
        "title_regex": r"(?:session (?:has )?expired|you have been logged out|page you requested cannot be displayed|temporarily unavailable)",
        "body_regex":  r"(?:session (?:has )?expired|temporarily unavailable|cannot be displayed)",
        "cancel_button_text": None,
    },
]

# Extract prior order numbers from the modal/page body.
SUBMISSION_ERROR_PRIOR_ORDER_REGEX = re.compile(
    r"Order\s*(?:Number|ID|#)[\s:\-]*([A-Z0-9]{4,})",
    re.IGNORECASE,
)


def _first_prior_order(text: str) -> str | None:
    """Return the first order number found in the given text, or None."""
    if not text:
        return None
    m = SUBMISSION_ERROR_PRIOR_ORDER_REGEX.search(text)
    return m.group(1) if m else None


def _ok(data: dict | None = None) -> dict:
    """Successful step result."""
    return {"ok": True, "message": None, "data": data or {}}


def _fail(message: str, data: dict | None = None) -> dict:
    """Failed step result — captures the portal message."""
    return {"ok": False, "message": message, "data": data or {}}


def _normalize_dob(dob: str) -> str:
    """Convert DOB to MM/DD/YYYY format for the portal.

    Handles:
      - YYYY-MM-DD (ISO from DB) → MM/DD/YYYY
      - MM/DD/YYYY (already correct) → pass through
      - Other formats → best-effort conversion
    """
    if not dob:
        return dob
    # YYYY-MM-DD → MM/DD/YYYY
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", dob)
    if m:
        return f"{int(m.group(2)):02d}/{int(m.group(3)):02d}/{m.group(1)}"
    # Already MM/DD/YYYY
    if re.match(r"^\d{1,2}/\d{1,2}/\d{4}$", dob):
        return dob
    return dob


class WebFormsClient:
    """Handles WebForms page DOM interactions for the Carelon portal.

    All selectors are HAR-validated from 3 actual submission recordings.
    Every method returns {"ok": bool, "message": str|None, "data": {}}.
    Portal messages are always captured — the system learns from volume.
    """

    def __init__(self, session: PlaywrightPortalSession):
        self.session = session
        self.page = session.page
        self.behavior = session.behavior
        self.reader = PageReader(session.page)

    # --- SOP Step 1: Member Search ---

    async def agree_to_terms(self) -> dict:
        """Click 'I Agree' on the terms/agreement page."""
        logger.info("Accepting terms of use")
        try:
            # Post-login terms page — Carelon can take 15-25s to render
            # under load. Bumped from 10s to 30s after observing wait_for_selector
            # timeouts on this exact step in production (29 Apr 2026).
            await self.page.wait_for_selector(SEL["agree_button"], timeout=30000)
            await self.behavior.click(SEL["agree_button"])
            await self.reader.wait_for_postback()
            return _ok()
        except Exception as e:
            messages = await self.reader.read_messages()
            return _fail(messages.error or f"Could not find agree button: {e}")

    async def search_member(
        self,
        first_name: str,
        last_name: str,
        dob: str,
        policy_num: str,
    ) -> dict:
        """Search for a member and select from results.

        Returns:
            {"ok": True, "data": {"results_count": N}} on success
            {"ok": False, "message": "portal error text"} on failure
        """
        # Convert DOB from YYYY-MM-DD to MM/DD/YYYY if needed
        dob = _normalize_dob(dob)
        logger.info(f"Searching member: {first_name} {last_name}, MemberID={policy_num}, DOB={dob}")

        # Wait for search form to be ready (critical for session reuse between cases)
        # HAR shows landing/member search page takes 18-23s to load
        try:
            await self.page.wait_for_selector(SEL["find_member"], state="visible", timeout=45000)
        except Exception:
            logger.warning("Member search form not ready — waiting for page load")
            await self.page.wait_for_load_state("domcontentloaded")
            await self.page.wait_for_selector(SEL["find_member"], state="visible", timeout=15000)

        # Date of Service = today
        today = date.today().strftime("%m/%d/%Y")
        await self.behavior.type_text(SEL["dos"], today)
        await self.behavior.type_text(SEL["member_number"], policy_num)
        await self.behavior.type_text(SEL["last_name"], last_name)
        await self.behavior.type_text(SEL["first_name"], first_name)
        await self.behavior.type_text(SEL["dob"], dob)

        # Click Find This Member
        await self.behavior.click(SEL["find_member"], action_type="buttonClick")
        await self.reader.wait_for_postback()

        # Read what the portal says
        messages = await self.reader.read_messages()
        if messages.error:
            return _fail(
                messages.error,
                data={"searched": {"first_name": first_name, "last_name": last_name, "policy_num": policy_num}},
            )

        # After search, the portal either:
        #   a) Shows a results grid (multiple matches) → select the first one
        #   b) Auto-selects (single match) → goes straight to service category page
        has_grid = await self.reader.has_results("member")

        if has_grid:
            # Multiple results — return count without auto-selecting.
            # Caller (portal_compiler) iterates rows to find an eligible plan.
            count = await self.reader.results_count("member")
            logger.info(f"Member search returned {count} result(s) in grid")

            return _ok({"results_count": count, "auto_selected": False, "grid_visible": True})

        # No grid — check if portal auto-selected (service category page)
        # Look for member name on the page (confirms member was found)
        page_text = await self.page.evaluate("() => document.body.innerText.substring(0, 2000)")
        page_lower = (page_text or "").lower()

        if last_name.lower() in page_lower and first_name.lower() in page_lower:
            logger.info(f"Member auto-selected: {first_name} {last_name}")
            return _ok({"results_count": 1, "auto_selected": True, "info": messages.info})

        # Neither grid nor member name found — member truly not found
        return _fail(
            "Member not found — no results returned",
            data={"searched": {"first_name": first_name, "last_name": last_name, "policy_num": policy_num}},
        )

    # --- SOP Step 1a-multi: Select a specific member from results grid ---

    async def select_member_at_index(self, index: int = 0) -> dict:
        """Select a specific member/plan row from the search results grid.

        ASP.NET GridView rows use ctl02 for first data row, ctl03 for second, etc.
        (ctl01 is typically the header row.)

        Args:
            index: 0-based index into data rows (0 = first result row)

        Returns:
            {"ok": True, "data": {...}} or {"ok": False, "message": ...}
        """
        ctl_num = f"{index + 2:02d}"
        selector = f"[id*='gvSearchMembers_ctl{ctl_num}_cmdSelectMember']"
        logger.info(f"Selecting member result at index {index} (ctl{ctl_num})")

        link = await self.page.query_selector(selector)
        if not link:
            # Fallback: try nth cmdSelectMember link
            all_links = await self.page.query_selector_all(
                "[id*='gvSearchMembers'] [id*='cmdSelectMember']"
            )
            if index < len(all_links):
                selector = f"[id*='gvSearchMembers'] [id*='cmdSelectMember'] >> nth={index}"
                logger.info(f"Fallback: using nth={index} selector")
            else:
                return _fail(f"No selectable member link at index {index}")

        await self.behavior.click(selector, action_type="searchResult")
        await self.reader.wait_for_postback()

        post_messages = await self.reader.read_messages()
        if post_messages.error:
            return _fail(post_messages.error)

        return _ok({"selected_index": index, "info": post_messages.info})

    async def navigate_back_to_member_results(self) -> dict:
        """Navigate back to the member search results grid after selecting a member.

        Uses browser back button — ASP.NET WebForms postbacks are full page reloads
        so the cached page should still have the grid with ViewState intact.

        Returns:
            {"ok": True} if grid is visible again, {"ok": False, "message": ...} otherwise
        """
        logger.info("Navigating back to member search results grid")
        import asyncio

        await self.page.go_back()
        await asyncio.sleep(1)

        # Wait for the grid to be visible again
        try:
            await self.page.wait_for_selector(
                "#asPrimary_ctl00_gvSearchMembers", state="visible", timeout=10000
            )
            return _ok({})
        except Exception:
            # Grid not visible — page back may have gone too far or ViewState expired
            logger.warning("Grid not visible after go_back — trying page reload approach")
            return _fail("Could not navigate back to member results grid")

    # --- SOP Step 1b: Extract Eligibility Details ---

    async def extract_eligibility_details(self) -> dict:
        """Extract eligibility details from the service category page.

        After member search, the page shows member info including:
          - Product/Carrier, Product Group, Employer Group ID
          - Effective dates for the current plan

        v157 — fast pre-check for Order Summary view. Some cases skip the
        eligibility page entirely: Carelon's portal redirects directly to
        a standalone Order Summary view (rendered by the PrintActivity
        user control) when the imaging center cannot submit (treating
        physician must initiate). That view has no `Effective` text, so
        the 30s wait below times out; the JS extractor then finds no
        "do not require Pre-Authorization" section and returns
        di_requires_auth=True (default). The case then cascades to a
        downstream "Select DI failed" HOLD that looks like a portal flake
        but is actually a physician-call signal we missed.

        Verified from prod HAR: this page has `#PrintActivity_ctl00_lblIneligible`
        but NO `Effective` text and NO eligibility two-section layout.
        Detecting that one element here lets us short-circuit cleanly.

        Returns:
            {"ok": True, "data": {"effective_date": ..., "plan_info": ..., ...}}

        For the Order Summary short-circuit, returns:
            {"ok": True, "data": {
                "page_type": "order_summary",
                "ineligible_text": str,
                "physician_initiation_required": bool,
                "di_requires_auth": False,
            }}
        """
        logger.info("Extracting eligibility details from service category page")

        # v157 pre-check — Order Summary detection BEFORE the 30s Effective wait.
        # If the portal redirected here (physician-call or hard no-auth),
        # bail out fast with a structured signal the compiler can route on.
        try:
            ineligible_text = await self.page.evaluate(
                """() => {
                    const sel = [
                        '#PrintActivity_ctl00_lblIneligible',
                        '#asPrimary_ctl00_lblIneligible',
                        'span[id$="_lblIneligible"]',
                    ];
                    for (const s of sel) {
                        const el = document.querySelector(s);
                        if (el && el.textContent && el.textContent.trim()) {
                            return el.textContent.trim();
                        }
                    }
                    return null;
                }"""
            )
            if ineligible_text:
                low = ineligible_text.lower()
                physician_call = (
                    "treating physician about initiating" in low
                    or "carelon order number may be required" in low
                    or "contact the treating physician" in low
                )
                logger.info(
                    f"Order Summary view detected — physician_call={physician_call} "
                    f"text={ineligible_text[:120]!r}"
                )
                return {
                    "ok": True,
                    "data": {
                        "page_type": "order_summary",
                        "ineligible_text": ineligible_text[:400],
                        "physician_initiation_required": physician_call,
                        "di_requires_auth": False,
                    },
                }
        except Exception as e:
            logger.warning(f"v157 Order Summary pre-check failed (non-fatal): {e}")

        # Wait for page to have member info loaded — eligibility page renders
        # post-postback and has been observed to take 15-25s under Carelon load.
        # Bumped from 10s to 30s; falls through to domcontentloaded on timeout.
        try:
            await self.page.wait_for_selector("text=Effective", state="visible", timeout=30000)
        except Exception:
            await self.page.wait_for_load_state("domcontentloaded")

        details = await self.page.evaluate("""
            () => {
                const result = {
                    effective_date: null,
                    plan_info: null,
                    member_text: null,
                };

                // Read the full page text to capture all displayed info
                const bodyText = document.body ? document.body.innerText : '';
                result.member_text = bodyText.substring(0, 3000);

                // Look for effective date patterns in the page
                // Common patterns: "Effective Date: MM/DD/YYYY", "Eff Date", "Effective"
                const effMatch = bodyText.match(
                    /(?:effective|eff\\.?)[\\s:]*date[\\s:]*([\\d]{1,2}\\/[\\d]{1,2}\\/[\\d]{4})/i
                );
                if (effMatch) result.effective_date = effMatch[1];

                // Also try: "MM/DD/YYYY-MM/DD/YYYY" or with spaces/dashes
                // Always overwrite effective + set term from range
                const rangeMatch = bodyText.match(
                    /(\\d{1,2}\\/\\d{1,2}\\/\\d{4})\\s*[-–—]\\s*(\\d{1,2}\\/\\d{1,2}\\/\\d{4})/
                );
                if (rangeMatch) {
                    result.effective_date = rangeMatch[1];
                    result.term_date = rangeMatch[2];
                }

                // Look for labeled fields in table/span elements
                const labels = document.querySelectorAll('td, th, span, label');
                for (const el of labels) {
                    const text = (el.textContent || '').trim().toLowerCase();
                    if (text.includes('product') && text.includes('carrier')) {
                        const next = el.nextElementSibling;
                        if (next) result.plan_info = next.textContent.trim();
                    }
                }

                // Try reading specific ASP.NET label elements for eligibility info
                const eligSelectors = [
                    '[id*="lblEffective"]', '[id*="lblEligibility"]',
                    '[id*="lblPlanEffDate"]', '[id*="lblProduct"]',
                    '[id*="lblCarrier"]', '[id*="lblEff"]',
                    '[id*="EffDate"]', '[id*="effDate"]',
                ];
                const eligData = {};
                for (const sel of eligSelectors) {
                    const els = document.querySelectorAll(sel);
                    for (const el of els) {
                        const t = (el.textContent || '').trim();
                        if (t) eligData[el.id || sel] = t;
                    }
                }
                if (Object.keys(eligData).length > 0) {
                    result.elig_fields = eligData;
                }

                // Check if DI requires pre-auth for this member's plan.
                // The eligibility page lists services in two sections:
                //   1. "may require a Pre-Authorization" (listed first)
                //   2. "do not require Pre-Authorization" (listed second)
                // If "Diagnostic Imaging" appears AFTER the "do not require" marker,
                // this member's plan doesn't need DI pre-auth.
                const lowerText = bodyText.toLowerCase();
                const noAuthIdx = lowerText.indexOf('do not require');
                const diIdx = lowerText.indexOf('diagnostic imaging');
                // DI requires auth if: no "do not require" section, OR DI appears before it
                result.di_requires_auth = (noAuthIdx === -1) || (diIdx === -1) || (diIdx < noAuthIdx);

                return result;
            }
        """)

        logger.info(f"Eligibility details: effective_date={details.get('effective_date')}, term_date={details.get('term_date')}, di_requires_auth={details.get('di_requires_auth')}")
        if details.get('elig_fields'):
            logger.info(f"Eligibility fields found: {details['elig_fields']}")

        return _ok(details)

    # --- SOP Step 2: Start Order ---

    async def select_diagnostic_imaging(self) -> dict:
        """Select 'Diagnostic Imaging' service type card on the service category page.

        After member search, the portal shows solution cards (Diagnostic Imaging,
        Cardiovascular, Sleep Management, etc.). We must click the DI card's
        actual checkbox/input — not just the text div.

        The portal uses ASP.NET checkboxes inside card containers. Clicking the
        card text doesn't trigger the proper JS event. We need to find the
        actual input element.
        """
        logger.info("Selecting Diagnostic Imaging service type")

        # Wait for service category cards to load — post-navigation page,
        # observed at 15-25s under Carelon load. Bumped from 10s → 30s.
        try:
            await self.page.wait_for_selector("h3.card-title", state="visible", timeout=30000)
        except Exception:
            await self.page.wait_for_load_state("domcontentloaded")

        # First, inspect the DOM to understand the card structure
        dom_info = await self.page.evaluate("""
            () => {
                const info = { inputs: [], cards: [], links: [] };

                // Find all inputs near "Diagnostic Imaging" text
                const allEls = document.querySelectorAll('*');
                for (const el of allEls) {
                    const ownText = Array.from(el.childNodes)
                        .filter(n => n.nodeType === 3)
                        .map(n => n.textContent.trim())
                        .join(' ');
                    if (ownText.includes('Diagnostic Imaging')) {
                        info.cards.push({
                            tag: el.tagName,
                            id: el.id,
                            className: el.className,
                            parent_id: el.parentElement ? el.parentElement.id : null,
                            parent_class: el.parentElement ? el.parentElement.className : null,
                        });
                    }
                }

                // Find all checkboxes and radios on the page
                const inputs = document.querySelectorAll(
                    'input[type="checkbox"], input[type="radio"]'
                );
                for (const inp of inputs) {
                    info.inputs.push({
                        tag: inp.tagName,
                        type: inp.type,
                        id: inp.id,
                        name: inp.name,
                        value: inp.value,
                        checked: inp.checked,
                        visible: inp.offsetParent !== null,
                    });
                }

                // Find any links/anchors that might be the clickable card element
                const links = document.querySelectorAll('a[id*="Diagnostic"], a[href*="Diagnostic"]');
                for (const a of links) {
                    info.links.push({ id: a.id, href: a.href, text: a.textContent.trim().substring(0, 50) });
                }

                return info;
            }
        """)
        logger.info(f"DI card DOM inspection: cards={dom_info.get('cards', [])}")
        logger.info(f"DI card inputs: {dom_info.get('inputs', [])}")

        # Strategy 1: Find checkbox/radio associated with Diagnostic Imaging
        clicked = await self.page.evaluate("""
            () => {
                // Find checkbox/radio inputs related to Diagnostic Imaging
                const inputs = document.querySelectorAll(
                    'input[type="checkbox"], input[type="radio"]'
                );
                for (const inp of inputs) {
                    const id = (inp.id || '').toLowerCase();
                    const name = (inp.name || '').toLowerCase();
                    const val = (inp.value || '').toLowerCase();

                    // Check if this input is for Diagnostic Imaging
                    if (id.includes('diagnostic') || name.includes('diagnostic')
                        || val.includes('diagnostic') || id.includes('rbdiagnostic')) {
                        inp.checked = true;
                        inp.click();
                        inp.dispatchEvent(new Event('change', { bubbles: true }));
                        inp.dispatchEvent(new Event('click', { bubbles: true }));
                        return { method: 'input', id: inp.id, type: inp.type };
                    }

                    // Also check associated label
                    const label = document.querySelector('label[for="' + inp.id + '"]');
                    if (label && label.textContent.includes('Diagnostic Imaging')) {
                        inp.checked = true;
                        inp.click();
                        inp.dispatchEvent(new Event('change', { bubbles: true }));
                        return { method: 'label_input', id: inp.id };
                    }
                }

                // Strategy 2: Find the card container and click its inner clickable element
                // Look for td/div containers with "Diagnostic Imaging" that have onclick
                const containers = document.querySelectorAll('td, div, span');
                for (const c of containers) {
                    if (c.onclick && c.textContent.includes('Diagnostic Imaging')) {
                        c.click();
                        return { method: 'onclick_container', tag: c.tagName, id: c.id };
                    }
                }

                // Strategy 3: Try __doPostBack style links
                const links = document.querySelectorAll('a');
                for (const a of links) {
                    const href = a.getAttribute('href') || '';
                    if (href.includes('Diagnostic') || (a.textContent || '').includes('Diagnostic Imaging')) {
                        a.click();
                        return { method: 'link', id: a.id, text: a.textContent.substring(0, 30) };
                    }
                }

                return null;
            }
        """)

        if clicked:
            logger.info(f"Selected Diagnostic Imaging: {clicked}")
            await self.behavior.think("formField")
            # Wait for any postback triggered by the selection
            try:
                await self.reader.wait_for_postback()
            except Exception:
                pass
            return _ok(clicked)

        # Strategy 4: Fallback — set the hidden field directly and force-click the button
        logger.warning("Could not find DI input, setting hidden field directly")
        phone_val = await self.page.evaluate("""
            () => {
                const phone = document.querySelector('#txbPhone');
                return phone ? phone.value || '' : '';
            }
        """)
        hidden_val = f"RbDiagnosticImaging;{phone_val};M;0;;;"
        await self.page.evaluate(f"""
            () => {{
                const hdn = document.getElementById('asPrimary_ctl00_hdnSelectedType');
                if (hdn) {{
                    hdn.value = '{hidden_val}';
                    return true;
                }}
                return false;
            }}
        """)
        logger.info(f"Set hdnSelectedType = {hidden_val}")
        return _ok({"method": "hidden_field_fallback"})

    async def extract_patient_phone(self) -> dict:
        """Extract patient mobile phone number from the phone field.

        The phone field (#txbPhone) appears after selecting Diagnostic Imaging.
        We read whatever value is pre-filled (if any).

        Returns:
            {"ok": True, "data": {"phone": "..."}}
        """
        logger.info("Extracting patient phone number")
        try:
            phone_val = await self.page.evaluate("""
                () => {
                    const phoneField = document.querySelector('#txbPhone');
                    return phoneField ? phoneField.value || '' : null;
                }
            """)
            logger.info(f"Patient phone: {phone_val or '(empty)'}")
            return _ok({"phone": phone_val})
        except Exception as e:
            logger.warning(f"Could not read phone field: {e}")
            return _ok({"phone": None})

    async def start_order_request(self) -> dict:
        """Click 'Start Order Request' (cmdContinue) after service type is selected.

        The button may be hidden if the portal's JS didn't register the DI
        selection. We try normal click first, then force click, then JS submit.
        """
        logger.info("Clicking Start Order Request")

        # Try 1: Normal click (button visible)
        try:
            btn = await self.page.query_selector(SEL["start_order"])
            if btn:
                visible = await btn.is_visible()
                if visible:
                    await self.behavior.click(SEL["start_order"], action_type="buttonClick")
                    await self.reader.wait_for_postback()
                    messages = await self.reader.read_messages()
                    if messages.error:
                        return _fail(messages.error)
                    return _ok({"info": messages.info, "method": "normal_click"})
        except Exception:
            pass

        # Try 2: Force click (button exists but hidden)
        logger.info("Button hidden — trying force click")
        try:
            await self.page.click(SEL["start_order"], force=True)
            await self.reader.wait_for_postback()
            messages = await self.reader.read_messages()
            if messages.error:
                return _fail(messages.error)
            return _ok({"info": messages.info, "method": "force_click"})
        except Exception:
            pass

        # Try 3: JS form submit
        logger.info("Force click failed — trying JS submit")
        try:
            await self.page.evaluate("""
                () => {
                    const btn = document.getElementById('cmdContinue');
                    if (btn) {
                        btn.style.display = 'inline';
                        btn.style.visibility = 'visible';
                        btn.click();
                        return true;
                    }
                    return false;
                }
            """)
            await self.reader.wait_for_postback()
            messages = await self.reader.read_messages()
            if messages.error:
                return _fail(messages.error)
            return _ok({"info": messages.info, "method": "js_submit"})
        except Exception as e:
            messages = await self.reader.read_messages()
            return _fail(messages.error or f"Start order failed: {e}")

    # --- SOP Step 3: Check Existing Auths ---

    async def extract_existing_auths(self) -> dict:
        """Extract all existing authorization details from the Member History grid.

        The portal shows: "Please verify the list of Order requests below
        to ensure you are not entering a duplicate case."

        Grid columns: Order ID, Order Status, Date of Service,
        Exam Description, Ordering Provider, Outcome, Reason.

        Returns:
            {"ok": True, "data": {"auths": [...], "count": N}}
        """
        logger.info("Extracting existing authorizations from Member History grid")

        # Wait for page to settle after Start Order postback
        await self.page.wait_for_load_state("domcontentloaded")

        # Check what page we're on
        page_text = await self.page.evaluate("() => document.body.innerText.substring(0, 500)")
        logger.info(f"check_existing_auths page_text (first 300): {page_text[:300]}")

        # Check if portal says "does not require an Order ID" — no auth needed
        # Be specific: only match the exact portal message, not generic page text
        no_auth_phrases = [
            "does not require pre-authorization",
            "does not require an order",
            "pre-authorization is not required",
        ]
        if any(phrase in page_text.lower() for phrase in no_auth_phrases):
            logger.warning(f"Portal says no auth required for this member/procedure")
            return _ok({
                "auths": [], "count": 0, "skipped": True,
                "no_auth_required": True,
                "portal_message": page_text[:300],
            })
        if "order summary" in page_text.lower():
            logger.warning(f"Order summary detected — may be stale session")
            return _ok({
                "auths": [], "count": 0, "skipped": True,
                "no_auth_required": True,
                "portal_message": page_text[:300],
            })

        # Already on provider search?
        is_provider_page = await self.page.query_selector(SEL["prov_search_type_tin_npi"])
        if is_provider_page:
            logger.info("Portal skipped auths page — already on provider search")
            return _ok({"auths": [], "count": 0, "skipped": True, "already_on_provider": True})

        # Wait for Next button (auths page indicator).
        # Paginated grids (patients with many prior auths — e.g. Nina Castillo
        # with 28 records across 3 pages) take longer to fully render, so
        # give the button up to 45s to appear.
        try:
            await self.page.wait_for_selector(SEL["next_button"], timeout=45000)
        except Exception:
            # Still not on auths page — check provider page one more time
            is_provider_page = await self.page.query_selector(SEL["prov_search_type_tin_npi"])
            if is_provider_page:
                logger.info("Portal skipped auths — on provider search after wait")
                return _ok({"auths": [], "count": 0, "skipped": True, "already_on_provider": True})

            # Re-read page text — we may be on a paginated auths page with
            # many records where the Next button didn't materialize in time.
            # If we detect the auths page markers, HOLD instead of silently
            # skipping (the old behavior caused the downstream provider_search
            # phase to fail with "Provider search page did not load").
            current_text = ""
            try:
                current_text = await self.page.evaluate(
                    "() => document.body.innerText.substring(0, 1000)"
                )
            except Exception:
                pass

            try:
                await self.page.screenshot(path="/tmp/ronexa_unknown_page_after_start.png")
            except Exception:
                pass

            is_auths_page = (
                "Member History" in current_text
                or "Records Found" in current_text
                or ("Order Request" in current_text and "duplicate" in current_text.lower())
            )
            if is_auths_page:
                logger.warning(
                    "On auths page but Next button didn't appear within 45s — HOLD"
                )
                return _fail(
                    "Existing auths page rendered but Next button not clickable within timeout. "
                    "Patient may have a large auth history requiring manual navigation."
                )

            logger.warning(f"Auths page not detected — page text: {current_text[:200]}")
            return _ok({"auths": [], "count": 0, "skipped": True})

        has_existing = await self.reader.has_results("exams")
        if not has_existing:
            logger.info("No existing authorizations found")
            return _ok({"auths": [], "count": 0})

        # Extract all rows from the Member History grid
        auths = await self.page.evaluate("""
            () => {
                const grid = document.querySelector('#asPrimary_ctl00_gvMemberExams');
                if (!grid) return [];

                const rows = grid.querySelectorAll('tr');
                const results = [];

                // Skip header row (index 0)
                for (let i = 1; i < rows.length; i++) {
                    const cells = rows[i].querySelectorAll('td');
                    if (cells.length >= 7) {
                        results.push({
                            order_id: (cells[0].textContent || '').trim(),
                            order_status: (cells[1].textContent || '').trim(),
                            date_of_service: (cells[2].textContent || '').trim(),
                            exam_description: (cells[3].textContent || '').trim(),
                            ordering_provider: (cells[4].textContent || '').trim(),
                            outcome: (cells[5].textContent || '').trim(),
                            reason: (cells[6].textContent || '').trim(),
                        });
                    }
                }
                return results;
            }
        """)

        logger.info(f"Found {len(auths)} existing authorizations")
        for auth in auths:
            logger.info(
                f"  Auth {auth['order_id']}: {auth['exam_description']} "
                f"({auth['date_of_service']}) — {auth['outcome']}"
            )

        return _ok({"auths": auths, "count": len(auths)})

    async def extract_ineligible_message(self) -> dict:
        """Read the portal's "ineligible" span and classify its meaning.

        Carelon uses the SAME element ID (`*_lblIneligible`) on multiple
        pages with different text per case outcome:

          • "A Carelon Order number may be required for this member.
             Please contact the treating physician about initiating the
             Carelon Order Request process."
            → imaging center can't submit; treating physician's office
              must initiate. Rep needs to CALL the physician.
            → `physician_initiation_required=True`

          • "DI does not require pre-authorization for this member's plan"
            → genuine no-auth; case is done.
            → `true_no_auth=True`

        v155 — introduced because the physician-initiation cases (Ian
        Lawler 15517158 / 17578976) previously lumped into NO_AUTH_REQUIRED
        and hid in the Completed tab even though the rep still had to call.

        Returns:
            {
              "ok": True,
              "present": bool,            # was the span found?
              "text": str,                # the span text (≤400 chars)
              "physician_initiation_required": bool,
              "true_no_auth": bool,
            }

        Safe to call defensively at any NO_AUTH-style detection site —
        returns `present=False` when nothing matches, so callers can
        fall through to existing logic.
        """
        try:
            text = await self.page.evaluate(
                """() => {
                    const selectors = [
                        '#PrintActivity_ctl00_lblIneligible',
                        '#asPrimary_ctl00_lblIneligible',
                        'span[id$="_lblIneligible"]',
                    ];
                    for (const sel of selectors) {
                        const el = document.querySelector(sel);
                        if (el && el.textContent && el.textContent.trim()) {
                            return el.textContent.trim();
                        }
                    }
                    return '';
                }"""
            )
        except Exception as e:
            logger.warning(f"extract_ineligible_message: page.evaluate failed: {e}")
            return {"ok": False, "present": False, "error": str(e)[:200]}

        if not text:
            return {"ok": True, "present": False}

        low = text.lower()
        physician_call = (
            "treating physician about initiating" in low
            or "carelon order number may be required" in low
            or "contact the treating physician" in low
        )
        true_no_auth = (
            not physician_call
            and (
                "does not require pre-authorization" in low
                or "does not require an order" in low
                or "pre-authorization is not required" in low
            )
        )
        logger.info(
            f"extract_ineligible_message: physician_call={physician_call} "
            f"true_no_auth={true_no_auth} text={text[:120]!r}"
        )
        return {
            "ok": True,
            "present": True,
            "text": text[:400],
            "physician_initiation_required": physician_call,
            "true_no_auth": true_no_auth,
        }

    async def click_next_after_auths(self) -> dict:
        """Click Next on the existing authorizations page.

        Call this after extract_existing_auths() and after the LLM
        has confirmed no duplicate CPT exists.
        """
        logger.info("Clicking Next on existing auths page")
        try:
            # Wait for Next button — existing-auths page renders post-postback
            # and the button appears after the auth grid finishes loading. 10s
            # was observed insufficient under Carelon load. Bumped → 30s.
            await self.page.wait_for_selector(SEL["next_button"], state="visible", timeout=30000)
            await self.behavior.click(SEL["next_button"], action_type="buttonClick")
            await self.reader.wait_for_postback()

            messages = await self.reader.read_messages()
            if messages.error:
                return _fail(messages.error)

            return _ok({"info": messages.info})
        except Exception as e:
            return _fail(f"Could not click Next: {e}")

    # --- SOP Step 4: Provider Search ---

    async def search_provider(
        self,
        referring_npi: str,
        state: str = "",
        fax: str = "",
        match_address: str = "",
        match_name: str = "",
    ) -> dict:
        """Search for referring provider by NPI, extract results, select best match, enter fax.

        After NPI search, extracts all provider results with addresses.
        Matching priority:
          1. Address match — `match_address` from RIS (disambiguates locations)
          2. Name match — `match_name` ("First Last") from RIS (confirmation/fallback)
          3. Default first — select first result

        After selecting a provider, the portal shows a fax number modal.
        If fax is provided, it's entered and saved. Otherwise clicks "Fax Unavailable".

        Returns:
            {"ok": True, "data": {"results_count": N, "providers": [...], "selected_index": I}}
        """
        logger.info(f"Searching provider NPI={referring_npi} in state={state}")

        # Wait for provider search form — state-based waiting + inline retry
        try:
            await self._wait_for_provider_search_page()
        except Exception as e:
            # Capture diagnostic info for debugging
            page_url = self.page.url
            page_title = ""
            try:
                page_title = await self.page.title()
                await self.page.screenshot(path="/tmp/ronexa_provider_search_fail.png")
            except Exception:
                pass
            logger.error(
                f"Provider search page did not load. URL={page_url}, "
                f"title={page_title}, error={e}"
            )
            return _fail(
                f"Portal error: Provider search page did not load after transition "
                f"(URL: {page_url[:80]})"
            )

        # Select "TIN or NPI" search type radio — senior rep goes straight to NPI
        await self.behavior.click(SEL["prov_search_type_tin_npi"], action_type="formField")

        # Select NPI radio
        await self.behavior.click(SEL["prov_npi_radio"], action_type="formField")

        # Enter NPI
        await self.behavior.type_text(SEL["prov_npi_field"], referring_npi)

        # Select state (only if provided — NPI is sufficient for search)
        if state:
            await self.behavior.select_option(SEL["prov_state"], state)

        # Click Search
        await self.behavior.click(SEL["prov_search_btn"], action_type="buttonClick")
        await self.reader.wait_for_postback()

        # State Wait: let the portal finish rendering results
        try:
            await self.page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            logger.warning("Provider search: networkidle timeout after search — continuing")

        # Read what the portal says
        messages = await self.reader.read_messages()
        if messages.error:
            return _fail(messages.error, data={"npi": referring_npi, "state": state})

        # Check for results — retry once if grid not rendered yet
        has_results = await self.reader.has_results("provider")
        if not has_results:
            # Grid might still be rendering — settle and check once more
            await self.page.wait_for_timeout(3000)
            try:
                await self.page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass
            has_results = await self.reader.has_results("provider")

        if not has_results:
            return _fail(
                messages.error or f"Provider NPI {referring_npi} not found in {state}",
                data={"npi": referring_npi, "state": state},
            )

        count = await self.reader.results_count("provider")

        # Extract all provider results with addresses
        providers = await self._extract_provider_results()
        logger.info(f"Found {len(providers)} provider result(s):")
        for i, p in enumerate(providers):
            logger.info(
                f"  [{i}] {p.get('name', '?')} — {p.get('address', '?')}, "
                f"{p.get('city', '?')} ({p.get('specialty', '?')})"
            )

        # Validate we actually have selectable providers
        if not providers:
            return _fail(
                f"Provider NPI {referring_npi} search returned grid but no selectable rows in {state}",
                data={"npi": referring_npi, "state": state},
            )

        # Select provider: address → name → default first
        selected_index = 0
        match_method = "single_result" if len(providers) == 1 else "default_first"

        if len(providers) > 1:
            # Priority 1: Address match (disambiguates locations)
            if match_address:
                idx = self._match_provider_by_address(providers, match_address)
                # _match_provider_by_address returns 0 as default, so check if it
                # actually matched by verifying address appears in the result
                addr_lower = match_address.lower().strip()
                candidate_addr = (providers[idx].get("address") or "").lower()
                if addr_lower in candidate_addr or candidate_addr in addr_lower or idx > 0:
                    selected_index = idx
                    match_method = "address"
                    logger.info(f"Address match at index {idx}: {providers[idx].get('address')}")

            # Priority 2: Name match (when address unavailable or didn't match)
            if match_method == "default_first" and match_name:
                idx = self._match_provider_by_name(providers, match_name)
                if idx >= 0:
                    selected_index = idx
                    match_method = "name"
                    logger.info(f"Name match at index {idx}: {providers[idx].get('name')}")

            if match_method == "default_first":
                logger.info("No address or name match — selecting first provider")

        # Click the selected provider's link
        try:
            # The provider grid is rendered post-postback after the search
            # submit and is one of Carelon's slower pages under load. Pre-wait
            # explicitly so the 10s default inside `behavior.click` doesn't
            # bite us before the grid finishes rendering.
            await self.page.wait_for_selector(
                "[id*='gvSearchProviders'] [id*='lnkBtnName']",
                state="visible",
                timeout=30000,
            )
            # Build selector for the specific row
            if selected_index == 0:
                # First result — use simple selector
                await self.behavior.click(
                    "[id*='gvSearchProviders'] [id*='lnkBtnName']",
                    action_type="searchResult",
                )
            else:
                # Specific row — click by index via JS
                await self.page.evaluate(f"""
                    () => {{
                        const links = document.querySelectorAll(
                            "[id*='gvSearchProviders'] [id*='lnkBtnName']"
                        );
                        if (links[{selected_index}]) {{
                            links[{selected_index}].click();
                            return true;
                        }}
                        return false;
                    }}
                """)
            await self.reader.wait_for_postback()
        except Exception as e:
            return _fail(f"Could not select provider: {e}")

        post_messages = await self.reader.read_messages()
        if post_messages.error:
            return _fail(post_messages.error)

        # Handle the fax popup that appears after provider selection.
        # Live testing (April 7 2026) confirmed: portal shows "Ordering Provider
        # Fax Number" popup. Clinical SPA won't initialize until it's dismissed.
        await self._handle_fax_modal(fax=fax)

        selected = providers[selected_index] if selected_index < len(providers) else None
        return _ok({
            "results_count": count,
            "providers": providers,
            "selected_index": selected_index,
            "selected_provider": selected,
            "match_method": match_method,
            "info": post_messages.info,
            # Provider match detail for flow check card
            "provider_match": {
                "name": selected.get("name") if selected else None,
                "address": selected.get("address") if selected else None,
                "match_method": match_method,
                "results_count": count,
                "selected_index": selected_index,
                "fax_entered": fax or None,
                "ris_address": match_address or None,
                "ris_name": match_name or None,
            },
        })

    async def _wait_for_provider_search_page(self) -> None:
        """Wait for provider search page with state-based waiting.

        Strategy 1: Wait for page state (networkidle) BEFORE checking DOM elements.
        Strategy 3: One inline retry — if element not found after state wait,
        wait again and check once more before failing.
        """
        selector = SEL["prov_search_type_tin_npi"]

        # State Wait: ensure page is fully settled before checking DOM
        await self.page.wait_for_load_state("domcontentloaded", timeout=30000)
        try:
            await self.page.wait_for_load_state("networkidle", timeout=30000)
        except Exception:
            logger.warning("Provider search: networkidle timeout — continuing with DOM check")

        # Now check for the element
        try:
            await self.page.wait_for_selector(selector, state="visible", timeout=15000)
            return  # Success
        except Exception:
            pass  # Fall through to retry

        # Retry: wait for another network settle + check again
        logger.warning("Provider search page not ready — retrying after settle")
        await self.page.wait_for_timeout(3000)
        try:
            await self.page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        # Final attempt — if this fails, the exception propagates to caller
        await self.page.wait_for_selector(selector, state="visible", timeout=15000)

    async def _extract_provider_results(self) -> list[dict]:
        """Extract all provider rows from the search results grid.

        Reads the grid headers dynamically, then maps each row's cells to
        the corresponding header. This handles any column order the portal uses.

        Returns list of dicts with provider details (name, address, city, state, zip, etc.)
        """
        return await self.page.evaluate("""
            () => {
                const grid = document.querySelector('#asPrimary_ctl00_gvSearchProviders');
                if (!grid) return [];

                const rows = grid.querySelectorAll('tr');
                if (rows.length < 2) return [];

                // Read headers from first row
                const headerCells = rows[0].querySelectorAll('th, td');
                const headers = [];
                for (const h of headerCells) {
                    headers.push((h.textContent || '').trim().toLowerCase());
                }

                // Map each data row
                const results = [];
                for (let i = 1; i < rows.length; i++) {
                    const cells = rows[i].querySelectorAll('td');
                    const row = { _raw_headers: headers };

                    for (let j = 0; j < cells.length; j++) {
                        const text = (cells[j].textContent || '').trim();
                        const header = j < headers.length ? headers[j] : 'col_' + j;

                        // Store raw column data by index for debugging
                        row['col_' + j] = text;

                        // Map known header patterns to standard keys
                        if (header.includes('name') || header.includes('provider')) {
                            row['name'] = text;
                        } else if (header.includes('address') && !header.includes('city')) {
                            row['address'] = text;
                        } else if (header.includes('city')) {
                            row['city'] = text;
                        } else if (header === 'state' || header === 'st' || header === 'st.') {
                            row['state'] = text;
                        } else if (header.includes('zip') || header.includes('postal')) {
                            row['zip'] = text;
                        } else if (header.includes('npi')) {
                            row['npi'] = text;
                        } else if (header.includes('phone') || header.includes('fax')) {
                            row['phone'] = text;
                        } else if (header.includes('specialty') || header.includes('type')) {
                            row['specialty'] = text;
                        }
                    }

                    // Fallback: if no named columns matched, try positional mapping
                    // Carelon grid: Name | Address | City | Specialty
                    if (!row['name'] && row['col_0']) row['name'] = row['col_0'];
                    if (!row['address'] && row['col_1']) row['address'] = row['col_1'];
                    if (!row['city'] && row['col_2']) row['city'] = row['col_2'];
                    if (!row['specialty'] && row['col_3']) row['specialty'] = row['col_3'];

                    // Also capture the full text of the row for fuzzy matching
                    row['_full_text'] = (rows[i].textContent || '').trim();
                    results.push(row);
                }
                return results;
            }
        """)

    def _match_provider_by_name(self, providers: list[dict], match_name: str) -> int:
        """Match provider by first + last name against the grid Name column.

        Portal grid shows name as "LAST, FIRST" (e.g. "TESFA, GANANA").
        We check if all name tokens from RIS appear in the grid name (case-insensitive).

        Returns index of first match, or -1 if no match.
        """
        tokens = [t.lower().strip() for t in match_name.split() if t.strip()]
        if not tokens:
            return -1

        for i, p in enumerate(providers):
            grid_name = (p.get("name") or "").lower()
            if all(token in grid_name for token in tokens):
                return i

        logger.info(f"No name match found for '{match_name}'")
        return -1

    def _match_provider_by_address(self, providers: list[dict], match_address: str) -> int:
        """Find the provider whose address best matches the given address.

        The portal grid shows: Name | Address (street) | City | Specialty.
        match_address can be a full address string like "2840 Legacy Dr, Frisco, TX 75034"
        or just the street portion.

        Uses case-insensitive substring matching. Returns the index of the
        best match, or 0 (first) if no match found.
        """
        if not match_address:
            return 0

        match_lower = match_address.lower().strip()

        # Try street address match (address column)
        for i, p in enumerate(providers):
            addr = (p.get("address") or "").lower().strip()
            if addr and (match_lower in addr or addr in match_lower):
                return i

        # Try full row text match (address might be in the full string)
        for i, p in enumerate(providers):
            full_text = (p.get("_full_text") or "").lower()
            if match_lower in full_text:
                return i

        # Try matching street number + street name (first 2 tokens)
        match_parts = match_lower.replace(",", " ").split()
        if len(match_parts) >= 2:
            street_key = " ".join(match_parts[:2])  # e.g. "2840 legacy"
            for i, p in enumerate(providers):
                addr = (p.get("address") or "").lower()
                if street_key in addr:
                    return i

        # Try city match as a secondary signal
        for i, p in enumerate(providers):
            city = (p.get("city") or "").lower().strip()
            if city and city in match_lower:
                return i

        logger.info(f"No address match found for '{match_address}' — defaulting to first")
        return 0

    def _score_facility_match(
        self,
        facility: dict,
        match_address: str,
        facility_name: str,
        zip_code: str,
    ) -> int:
        """Score a facility result for selection confidence. Higher = better match.

        Scoring:
          +100  address substring match (strongest — exact location)
          +80   street number + name token match
          +90   zip code found in result text (disambiguates multi-location)
          +60   2+ name keywords match facility name
          +30   1 name keyword match
          +40   city name found in match_address
        """
        score = 0
        addr = (facility.get("address") or "").lower()
        name = (facility.get("name") or "").lower()
        full_text = (facility.get("_full_text") or "").lower()

        # Address substring match (strongest signal)
        if match_address:
            match_lower = match_address.lower().strip()
            if match_lower in addr or addr in match_lower:
                score += 100
            else:
                # Street number + name tokens (e.g. "6957 plano" in "6957 W PLANO PKWY")
                parts = match_lower.replace(",", " ").replace("-", " ").split()
                if len(parts) >= 2:
                    street_key = " ".join(parts[:2])
                    if street_key in addr:
                        score += 80

        # Zip code in full result text (strong — disambiguates locations)
        if zip_code and zip_code in full_text:
            score += 90

        # Facility name keywords (medium — catches "Envision" + "Plano" in result name)
        if facility_name:
            name_lower = facility_name.lower()
            key_words = [w for w in name_lower.split() if len(w) > 3]
            matches = sum(1 for w in key_words if w in name)
            if matches >= 2:
                score += 60
            elif matches >= 1:
                score += 30

        # City match
        city = (facility.get("city") or "").lower()
        if match_address and city:
            if city in match_address.lower():
                score += 40

        return score

    async def _handle_fax_modal(self, fax: str = "") -> None:
        """Handle the 'Ordering Provider Fax Number' popup after provider selection.

        Live testing (April 7 2026) confirmed this popup appears after clicking
        a provider. It has a fax number input and a Save button. The clinical
        SPA will NOT initialize (GetCase returns null) until this popup is
        dismissed by filling the fax and clicking Save.
        """
        import asyncio

        logger.info(f"_handle_fax_modal: checking for fax popup (fax={fax})")

        # Wait briefly for the popup to render
        await asyncio.sleep(1)

        # Check if the fax field is visible (indicates popup is showing)
        fax_field = SEL["prov_fax"]  # #asPrimary_ctl00_txbFax
        try:
            fax_el = await self.page.wait_for_selector(
                fax_field, state="visible", timeout=5000
            )
        except Exception:
            logger.info("_handle_fax_modal: No fax popup detected — skipping")
            return

        if not fax_el:
            logger.info("_handle_fax_modal: Fax field not found — skipping")
            return

        logger.info("_handle_fax_modal: Fax popup detected!")

        # Strip fax to pure 10 digits — portal validates "Must be 10 digits"
        # and keeps the Save button disabled until the input is exactly 10 digits
        import re
        digits_only = re.sub(r'\D', '', fax) if fax else ""

        if not digits_only:
            # Try to extract from pre-filled value
            existing = await fax_el.input_value()
            digits_only = re.sub(r'\D', '', existing) if existing else ""

        if digits_only and len(digits_only) >= 10:
            digits_only = digits_only[:10]  # Take first 10 digits
            # Clear the field by selecting all and deleting — triggers portal JS events
            await fax_el.click()
            await self.page.keyboard.press("Control+a")
            await self.page.keyboard.press("Backspace")
            await asyncio.sleep(0.3)
            # Type digits one by one — this triggers keyup/input events that
            # the portal JS uses to validate and enable the Save button
            await fax_el.type(digits_only, delay=50)
            logger.info(f"_handle_fax_modal: Typed fax digits: {digits_only}")
        else:
            logger.warning(f"_handle_fax_modal: No valid 10-digit fax — digits='{digits_only}'")

        # Wait for portal JS validation to enable the Save button
        await asyncio.sleep(1)

        # The Save button is: <button id="save" type="button" class="button popup-buttons-disabled">Save</button>
        # It becomes enabled (class changes) once fax passes 10-digit validation
        save_clicked = False
        try:
            # Wait for Save button to become enabled (class loses "popup-buttons-disabled")
            save_btn = await self.page.wait_for_selector(
                "button#save:not(.popup-buttons-disabled)", timeout=3000
            )
            if save_btn:
                await save_btn.click()
                save_clicked = True
                logger.info("_handle_fax_modal: Clicked enabled Save button")
        except Exception:
            # If the enabled selector doesn't work, try clicking it directly
            logger.info("_handle_fax_modal: Save button still disabled, trying direct click")
            try:
                await self.page.evaluate("""() => {
                    const btn = document.querySelector('button#save');
                    if (btn) {
                        btn.classList.remove('popup-buttons-disabled');
                        btn.click();
                        return true;
                    }
                    return false;
                }""")
                save_clicked = True
                logger.info("_handle_fax_modal: Force-clicked Save via JS (removed disabled class)")
            except Exception as e:
                logger.warning(f"_handle_fax_modal: JS force-click failed: {e}")

        # Third fallback: dispatch click event directly
        if not save_clicked:
            try:
                await self.page.dispatch_event("button#save", "click")
                save_clicked = True
                logger.info("_handle_fax_modal: Dispatched click event on Save button")
            except Exception as e:
                logger.warning(f"_handle_fax_modal: dispatch_event click failed: {e}")

        if save_clicked:
            # Wait for the popup to close and page to settle
            await self.reader.wait_for_postback()
            await asyncio.sleep(0.5)
            # Verify modal actually closed
            try:
                await self.page.wait_for_selector(fax_field, state="hidden", timeout=3000)
            except Exception:
                # Modal still visible — try Enter key as last resort
                logger.warning("_handle_fax_modal: Modal still visible after Save — trying Enter")
                await self.page.keyboard.press("Enter")
                await asyncio.sleep(1)
            logger.info("_handle_fax_modal: Fax popup handled successfully")
        else:
            # All click methods failed — try Enter key as absolute last resort
            logger.warning("_handle_fax_modal: All Save click methods failed — trying Enter key")
            await self.page.keyboard.press("Enter")
            await asyncio.sleep(1)
            logger.error("_handle_fax_modal: Could NOT click Save — tried Enter as fallback")

    # --- ASP.NET Async Postback Helper ---

    async def postback_hdnaction(self, action_value: int, timeout_ms: int = 60000) -> dict:
        """Trigger a full-page form submission with hdnAction to transition pages.

        HAR-proven mechanism for page transitions in the Carelon portal:
          - hdnAction=20: clinical SPA → exam summary page
          - hdnAction=6:  exam summary → facility search page
          - hdnAction=17: exam review → order summary page

        The clinical SPA runs INSIDE Default.aspx — the hidden ASP.NET form
        with __VIEWSTATE and hdnAction still exists in the DOM even when the
        SPA has taken over the URL (e.g. /exam-entry, /pathway-questions).
        The SPA submits this hidden form to navigate back to WebForms.

        HAR shows this as a full document navigation (sec-fetch-dest: document),
        not an async XHR postback.
        """
        import asyncio
        logger.info(f"Triggering hdnAction={action_value} form submission")

        try:
            # Find the hidden form and set hdnAction — search broadly since
            # the SPA may alter the DOM structure
            setup = await self.page.evaluate(f"""() => {{
                // Search strategies for hdnAction field:
                // 1. By exact ID (ASP.NET underscore format)
                // 2. By name attribute (ASP.NET dollar format)
                // 3. By partial ID match
                // 4. Create it if not found (the form exists but field may be missing)
                let hdnAction = document.getElementById('asPrimary_ctl00_hdnAction');
                if (!hdnAction) {{
                    hdnAction = document.querySelector('[name="asPrimary$ctl00$hdnAction"]');
                }}
                if (!hdnAction) {{
                    hdnAction = document.querySelector('[id*="hdnAction"]');
                }}
                if (!hdnAction) {{
                    hdnAction = document.querySelector('input[name*="hdnAction"]');
                }}

                // Find the ASP.NET form
                const form = document.querySelector('form[action*="Default.aspx"]') ||
                             document.querySelector('form[id="aspnetForm"]') ||
                             document.forms[0];

                if (!form) {{
                    return {{ error: 'No form found on page', url: window.location.href }};
                }}

                // If hdnAction field doesn't exist, create it inside the form
                if (!hdnAction) {{
                    hdnAction = document.createElement('input');
                    hdnAction.type = 'hidden';
                    hdnAction.name = 'asPrimary$ctl00$hdnAction';
                    hdnAction.id = 'asPrimary_ctl00_hdnAction';
                    form.appendChild(hdnAction);
                }}

                hdnAction.value = '{action_value}';

                // Also set __ASYNCPOST (HAR shows it as 'true' in form data)
                let asyncPost = document.querySelector('[name="asPrimary$ctl00$__ASYNCPOST"]') ||
                                document.getElementById('asPrimary_ctl00___ASYNCPOST');
                if (!asyncPost) {{
                    asyncPost = document.createElement('input');
                    asyncPost.type = 'hidden';
                    asyncPost.name = 'asPrimary$ctl00$__ASYNCPOST';
                    form.appendChild(asyncPost);
                }}
                asyncPost.value = 'true';

                return {{
                    ok: true,
                    formAction: form.action || form.getAttribute('action'),
                    formId: form.id,
                    hdnActionFound: !!document.getElementById('asPrimary_ctl00_hdnAction') ||
                                    !!document.querySelector('[name="asPrimary$ctl00$hdnAction"]'),
                    fieldCount: form.elements.length,
                    hasViewState: !!form.querySelector('[name="__VIEWSTATE"]'),
                    url: window.location.href,
                }};
            }}""")

            logger.info(f"hdnAction={action_value} setup: {setup}")

            if isinstance(setup, dict) and setup.get("error"):
                return _fail(setup["error"])

            # Submit the form — HAR shows this as a full document navigation
            # Use page.evaluate to submit, then wait for navigation
            async with self.page.expect_navigation(
                wait_until="load", timeout=timeout_ms
            ):
                await self.page.evaluate("""() => {
                    const form = document.querySelector('form[action*="Default.aspx"]') ||
                                 document.querySelector('form[id="aspnetForm"]') ||
                                 document.forms[0];
                    if (form) form.submit();
                }""")

            # Wait for ViewState (confirms ASP.NET rendered the new page)
            # __VIEWSTATE is type="hidden" — use state="attached" not default "visible"
            try:
                await self.page.wait_for_selector("[name='__VIEWSTATE']", state="attached", timeout=timeout_ms)
            except Exception as e:
                err_name = type(e).__name__
                if "TargetClosedError" in err_name or "closed" in str(e).lower():
                    logger.warning(f"hdnAction={action_value}: page/browser closed during navigation")
                    raise
                logger.warning(f"hdnAction={action_value}: ViewState not found after navigation")

            # Settle time for client-side JS init (clinical SPA may re-load)
            await asyncio.sleep(2)

            # Verify we're still on the portal (not redirected to login)
            url = self.page.url
            logger.info(f"After hdnAction={action_value}: URL={url}")

            if "User Confirmation" in await self.page.title() or "login" in url.lower():
                return _fail("Session expired — redirected to login after postback")

            return _ok({"url": url})

        except Exception as e:
            logger.error(f"hdnAction={action_value} postback failed: {e}")
            return _fail(f"hdnAction postback failed: {e}")

    # --- SOP Step 8: Facility Search ---

    async def search_facility(
        self,
        center_npi: str,
        state: str = "",
        facility_name: str = "",
        match_address: str = "",
        zip_code: str = "",
        fax: str = "",
    ) -> dict:
        """Search for rendering facility by NPI + name, match by address, select.

        Uses CenterNPI, CenterDesc, CenterAddress, CenterState from MongoDB.
        Matching priority (when multiple results):
          1. Address match — CenterAddress from RIS (disambiguates locations)
          2. Default first — select first result

        If in-network search returns no results, tries OON (out-of-network) expansion.

        Returns:
            {"ok": True, "data": {"facility_match": {...}}} on success
            {"ok": False, "message": "..."} on failure
        """
        logger.info(
            f"Searching facility: NPI={center_npi}, name={facility_name}, "
            f"state={state}, address={match_address}"
        )

        # Wait for facility search page to load.
        # HAR shows hdnAction=6 page transition can take 25-35 seconds.
        try:
            await self.page.wait_for_selector(SEL["fac_advanced_link"], state="visible", timeout=45000)
        except Exception:
            await self.page.wait_for_load_state("domcontentloaded")
            # Give the page a second chance after DOMContentLoaded
            try:
                await self.page.wait_for_selector(SEL["fac_advanced_link"], state="visible", timeout=15000)
            except Exception:
                pass  # Fall through — click attempt below will produce a clear error

        # Click advanced search link
        try:
            await self.behavior.click(SEL["fac_advanced_link"], action_type="buttonClick")
            await self.reader.wait_for_postback()
        except Exception as e:
            return _fail(f"Could not open advanced facility search: {e}")

        # Enter NPI
        await self.behavior.type_text(SEL["fac_npi"], center_npi)

        # Enter facility name — use first word only for partial match
        # HAR shows real reps search with just one keyword (e.g., "envision")
        # to avoid mismatches on multi-word names
        if facility_name:
            short_name = facility_name.split()[0]  # "Envision" from "Envision Imaging Flower Mound"
            await self.behavior.type_text(SEL["fac_name"], short_name)

        # Select state (only if provided — NPI is sufficient for search)
        if state:
            await self.behavior.select_option(SEL["fac_state"], state)

        # Optional zip code
        if zip_code:
            await self.behavior.type_text(SEL["fac_zip"], zip_code)

        # Click Find
        await self.behavior.click(SEL["fac_find_btn"], action_type="buttonClick")
        await self.reader.wait_for_postback()

        # Read what the portal says
        messages = await self.reader.read_messages()
        if messages.error:
            return _fail(messages.error, data={"npi": center_npi, "state": state})

        # Check for results
        has_results = await self.reader.has_results("provider")

        # OON fallback: if in-network search returned nothing, expand to out-of-network
        if not has_results:
            try:
                oon_btn = await self.page.query_selector("[id*='cmdExpOONSearch']")
                if oon_btn:
                    logger.info("Facility not found in-network — expanding to OON search")
                    await self.behavior.click("[id*='cmdExpOONSearch']", action_type="buttonClick")
                    await self.reader.wait_for_postback()
                    has_results = await self.reader.has_results("provider")
            except Exception as e:
                logger.warning(f"OON expansion failed: {e}")

        # NPI-only retry: if NPI+name+zip returned nothing, try NPI+state only
        # (facility name/zip in portal may differ from MongoDB CenterDesc)
        if not has_results and (facility_name or zip_code):
            logger.info("Facility NPI+name+zip search failed — retrying with NPI+state only")
            try:
                # Clear name and zip fields, keep NPI and state
                await self.page.evaluate(f"""() => {{
                    const nameField = document.querySelector('{SEL["fac_name"]}');
                    if (nameField) nameField.value = '';
                    const zipField = document.querySelector('{SEL["fac_zip"]}');
                    if (zipField) zipField.value = '';
                }}""")
                await self.behavior.click(SEL["fac_find_btn"], action_type="buttonClick")
                await self.reader.wait_for_postback()
                has_results = await self.reader.has_results("provider")
                if not has_results:
                    # Try OON on NPI-only search too
                    try:
                        oon_btn = await self.page.query_selector("[id*='cmdExpOONSearch']")
                        if oon_btn:
                            logger.info("NPI-only not found in-network — expanding to OON")
                            await self.behavior.click("[id*='cmdExpOONSearch']", action_type="buttonClick")
                            await self.reader.wait_for_postback()
                            has_results = await self.reader.has_results("provider")
                    except Exception:
                        pass
            except Exception as e:
                logger.warning(f"NPI-only retry failed: {e}")

        if not has_results:
            return _fail(
                messages.error or f"Facility NPI {center_npi} not found in {state}",
                data={"npi": center_npi, "state": state, "facility_name": facility_name},
            )

        # Extract all facility results (reuses provider grid — same gvSearchProviders)
        facilities = await self._extract_provider_results()
        logger.info(f"Found {len(facilities)} facility result(s):")
        for i, f in enumerate(facilities):
            logger.info(
                f"  [{i}] {f.get('name', '?')} — {f.get('address', '?')}, "
                f"{f.get('city', '?')}"
            )

        if not facilities:
            return _fail(
                f"Facility NPI {center_npi} search returned grid but no selectable rows",
                data={"npi": center_npi, "state": state},
            )

        # Select best match using multi-criteria scoring
        selected_index = 0
        match_method = "single_result" if len(facilities) == 1 else "default_first"

        if len(facilities) > 1:
            scores = [
                self._score_facility_match(f, match_address, facility_name, zip_code)
                for f in facilities
            ]
            best_idx = max(range(len(scores)), key=lambda i: scores[i])
            best_score = scores[best_idx]

            logger.info(
                f"Facility scoring ({len(facilities)} results): "
                + ", ".join(f"[{i}]={s}" for i, s in enumerate(scores))
                + f" → best=[{best_idx}] score={best_score}"
            )

            if best_score >= 30:
                selected_index = best_idx
                match_method = f"scored_{best_score}"
                logger.info(
                    f"Facility selected by scoring: [{best_idx}] "
                    f"{facilities[best_idx].get('name')} at {facilities[best_idx].get('address')}"
                )
            else:
                # No confident match — HOLD. Wrong facility is worse than no submission.
                return _fail(
                    f"Facility NPI {center_npi}: {len(facilities)} results but no confident match "
                    f"(best score={best_score}). Expected: '{facility_name}' at '{match_address}'",
                    data={
                        "facilities": [
                            {"name": f.get("name"), "address": f.get("address"), "city": f.get("city")}
                            for f in facilities
                        ],
                        "expected_address": match_address,
                        "expected_name": facility_name,
                    },
                )
        elif len(facilities) == 1:
            logger.info(f"Single facility result: {facilities[0].get('name')}")

        # Click the selected facility's Select button — same pattern as the
        # provider grid above: the facility-results page renders post-search
        # and is sometimes slow under Carelon load. Pre-wait so behavior.click's
        # 10s default doesn't trip on a still-rendering page.
        try:
            await self.page.wait_for_selector(
                "[id*='gvSearchProviders'] [id*='cmdSelectFacility']",
                state="visible",
                timeout=30000,
            )
            if selected_index == 0:
                await self.behavior.click(
                    "[id*='gvSearchProviders'] [id*='cmdSelectFacility']",
                    action_type="searchResult",
                )
            else:
                # Click specific row by index via JS
                await self.page.evaluate(f"""
                    () => {{
                        const btns = document.querySelectorAll(
                            "[id*='gvSearchProviders'] [id*='cmdSelectFacility']"
                        );
                        if (btns[{selected_index}]) {{
                            btns[{selected_index}].click();
                            return true;
                        }}
                        return false;
                    }}
                """)
            await self.reader.wait_for_postback()
        except Exception as e:
            return _fail(f"Could not select facility: {e}")

        # Set fax hidden fields before clicking Continue
        # Reference: carelon-sub continue_after_facility() — portal requires these
        # hdnFaxMandatory=false bypasses the fax validation gate
        # hdnFaxEntered stores the fax for the order
        try:
            await self.page.evaluate(
                f"""() => {{
                    const faxMandatory = document.getElementById('asPrimary_ctl00_hdnFaxMandatory');
                    if (faxMandatory) faxMandatory.value = 'false';

                    const faxEntered = document.getElementById('asPrimary_ctl00_hdnFaxEntered');
                    if (faxEntered) faxEntered.value = '{fax}';
                }}"""
            )
            logger.info(f"Set fax hidden fields: hdnFaxMandatory=false, hdnFaxEntered={fax}")
        except Exception as e:
            logger.warning(f"Could not set fax hidden fields: {e}")

        # Click Continue on facility confirmation page — confirmed source of
        # production HOLDs at the 10s boundary (29 Apr 2026: Norma Hill,
        # Aditya Garg). Carelon's post-search confirmation page renders in
        # 15-25s under load. Bumped from 10s → 30s.
        try:
            await self.page.wait_for_selector(SEL["fac_continue"], timeout=30000)
            await self.behavior.click(SEL["fac_continue"], action_type="buttonClick")
            await self.reader.wait_for_postback()
        except Exception as e:
            messages = await self.reader.read_messages()
            return _fail(messages.error or f"Facility continue failed: {e}")

        post_messages = await self.reader.read_messages()
        if post_messages.error:
            return _fail(post_messages.error)

        selected = facilities[selected_index] if selected_index < len(facilities) else None
        return _ok({
            "info": post_messages.info,
            "facility_match": {
                "name": selected.get("name") if selected else None,
                "address": selected.get("address") if selected else None,
                "match_method": match_method,
                "results_count": len(facilities),
                "selected_index": selected_index,
                "fax_entered": fax or None,
                "center_address": match_address or None,
            },
        })

    # --- SOP Step 9: Submit ---

    async def submit_request(self) -> dict:
        """Click Submit This Request, detect blocking modals, extract confirmation.

        After the Submit postback we first probe for a blocking modal / error
        layout (duplicate-order review, criteria-not-met, session expired,
        portal error page). If we find one, we capture the message, cancel
        cleanly, and bubble up as a submission error — the case lands in the
        new SUBMISSION_ERROR state instead of a generic HOLD.

        Returns:
            {"ok": True, "data": {"order_id": ..., "status": ..., ...}} on success
            {"ok": True, "data": {"submission_error": {...}}} when the portal
                refused at submit step (not a process failure — still "ok" so
                the caller can route to SUBMISSION_ERROR cleanly)
            {"ok": False, "message": "portal error text"} on hard failure
        """
        logger.info("Submitting order request")
        try:
            await self.behavior.click(SEL["submit_request"], action_type="buttonClick")
            await self.reader.wait_for_postback(timeout_ms=45000)
        except Exception as e:
            messages = await self.reader.read_messages()
            return _fail(messages.error or f"Submit failed: {e}")

        # --- Blocking modal / error layout detection (runs BEFORE confirmation) ---
        err = await self.detect_submission_error()
        if err:
            logger.warning(
                f"Submission error detected: type={err.get('error_type')} "
                f"title={err.get('title')!r} prior={err.get('prior_order_id')}"
            )
            # Cancel cleanly so the worker session doesn't get stuck on the modal.
            await self._cancel_submission(err)
            return _ok({"submission_error": err})

        # Read any messages
        messages = await self.reader.read_messages()
        if messages.error:
            return _fail(messages.error)

        # Extract confirmation details
        confirmation = await self._extract_confirmation()
        confirmation["info"] = messages.info

        return _ok(confirmation)

    async def _extract_confirmation(self) -> dict:
        """Extract confirmation details from the result page.

        Two-stage strategy:
          1. Read the canonical DOM selectors. APPROVED outcomes typically
             render with these populated.
          2. If the status selector missed (common for PENDED outcomes where
             Carelon renders the confirmation page with non-standard markup),
             fall back to scanning the page body text for outcome keywords.
             This recovers legitimate PENDED/APPROVED/DENIED outcomes that
             would otherwise hit the "no confirmation captured" HOLD path
             (commit 3b79b76 introduced the HOLD; without this body-text
             fallback, any markup variation causes a false HOLD).
        """
        selectors_map = {
            "order_id": SEL["conf_order_id"],
            "status": SEL["conf_status"],
            "valid_from": SEL["conf_valid_from"],
            "valid_through": SEL["conf_valid_through"],
            "health_plan": SEL["conf_health_plan"],
        }

        result = await self.reader.read_multiple(selectors_map)

        # Page body text — fetched lazily once per call. None until we need
        # it. Multiple downstream branches (status fallback, order_id
        # fallback, determination_date scan) all share this single fetch.
        body: str | None = None

        async def _get_body() -> str:
            nonlocal body
            if body is not None:
                return body
            try:
                body = await self.page.evaluate(
                    "() => document.body ? document.body.innerText : ''"
                ) or ""
            except Exception as e:
                logger.warning(f"Confirmation body-scan: page.evaluate failed: {e}")
                body = ""
            return body

        # Body-text fallback: status selector missed → scan page body for
        # outcome keywords. The keyword scan is the same logic a rep would
        # use looking at the page. Order matters — denied is more specific
        # than just "review", and "pending review" is more specific than
        # bare "pended".
        if not result.get("status"):
            body_lower = (await _get_body()).lower()
            for keyword, status_label in (
                ("denied", "Denied"),
                ("pending review", "Pending Review"),
                ("pended", "Pended"),
                ("in progress", "In Progress"),
                ("approved", "Approved"),
            ):
                if keyword in body_lower:
                    logger.info(
                        f"Confirmation: status selector missed; "
                        f"scraped '{status_label}' from page body text"
                    )
                    result["status"] = status_label
                    result["status_source"] = "body_scan"
                    break

            # Try to scrape an order id via the body text too — Carelon
            # prefixes them with "Order #", "Order Number", or "Reference
            # Number". The captured group requires at least one digit
            # (otherwise greedy matches grab e.g. the literal word "Number"
            # from "Order Number: ORD-12345").
            if not result.get("order_id"):
                m = re.search(
                    r"(?:order\s*(?:#|number)?|reference\s+number)"
                    r"[:\s]*((?=[A-Z0-9-]*\d)[A-Z0-9-]{6,})",
                    (await _get_body()), re.IGNORECASE,
                )
                if m:
                    result["order_id"] = m.group(1)
                    result["order_id_source"] = "body_scan"
                    logger.info(
                        f"Confirmation: order_id scraped from body: {m.group(1)}"
                    )

        # Anticipated Determination Date — separate from valid_from (which is
        # "Scheduled Date of Service"). Body regex scan against the literal
        # label Carelon renders. This is the date the rep cares about for
        # PENDED outcomes — when Carelon expects to render the auth decision.
        # We always run this regex (even when other selectors hit) because
        # there's no DOM selector for this field today; the body text label
        # is the most stable extraction surface. APPROVED outcomes typically
        # don't show this label (decision is already rendered), so the regex
        # cleanly returns no match and determination_date stays NULL.
        if not result.get("determination_date"):
            m = re.search(
                r"anticipated\s+determination\s+date\s*[:.]?\s*(\d{1,2}/\d{1,2}/\d{2,4})",
                (await _get_body()), re.IGNORECASE,
            )
            if m:
                result["determination_date"] = m.group(1)
                logger.info(
                    f"Confirmation: determination_date scraped from body: {m.group(1)}"
                )

        logger.info(
            f"Confirmation: Order={result.get('order_id')}, "
            f"Status={result.get('status')}, "
            f"DeterminationDate={result.get('determination_date')}"
        )
        return result

    async def detect_submission_error(self) -> dict | None:
        """Scan the page after Submit for a blocking modal / error layout.

        Two-pass strategy: probe immediately, and if nothing matches, wait
        briefly and probe again. Carelon's duplicate-order modal can render
        1-2 seconds AFTER the postback completes (jQuery animation + AJAX
        load). The retry catches that window without adding meaningful
        latency to the happy path (probe returns immediately on a normal
        confirmation page).

        Returns None on no error detected. Otherwise returns a dict:

            {
              "error_type": "duplicate" | "criteria_not_met" | "portal_error" | "unknown",
              "title": str,
              "body_text": str,
              "prior_order_id": str|None,
              "matched_rule": str|None,
            }
        """
        err = await self._probe_submission_error()
        if err:
            return err
        # No error matched on first probe. Wait briefly in case a modal is
        # still animating in (Nikita Durden 17417574 — duplicate modal
        # rendered late, body_text on first probe didn't include the modal
        # text, fell through to HOLD with "no confirmation captured").
        import asyncio
        await asyncio.sleep(2)
        return await self._probe_submission_error()

    async def _probe_submission_error(self) -> dict | None:
        """Single-shot scan for a submission-error modal / interstitial.

        Strategy:
          1. Inspect the DOM for visible modals (jQuery UI / [role=dialog] /
             Kendo / common Carelon class patterns).
          2. Fall back to scanning the page body text.
          3. Match against SUBMISSION_ERROR_PATTERNS top-down; first hit wins.
          4. Extract the prior order number from whatever text we matched.

        bodyText cap was bumped 1600 → 8000 chars (v146). The Order Request
        Preview page has hundreds of chars of header content before the
        duplicate-modal text appears in DOM order; the old 1600 cap cut off
        before reaching the modal text, so the body-only fallback returned
        no match.
        """
        try:
            raw = await self.page.evaluate(
                r"""() => {
                    // Collect candidate modal containers — Carelon uses jQuery UI
                    // dialogs, Kendo windows, and a few custom popup divs. We also
                    // check common ARIA role markers and id-pattern selectors for
                    // Carelon's specific dialogs.
                    const selectors = [
                      '.ui-dialog:not(.ui-dialog-hidden)',
                      '[role="dialog"]',
                      '.modal.show',
                      '.popup:not([style*="display: none"])',
                      '.k-window',
                      '[id*="DuplicateOrder"]',
                      '[id*="duplicateOrder"]',
                      'div[class*="Modal"]',
                      'div[class*="modal-dialog"]',
                    ];
                    const dialogs = [];
                    const seen = new Set();
                    for (const sel of selectors) {
                      let els;
                      try { els = document.querySelectorAll(sel); } catch (e) { continue; }
                      for (const el of els) {
                        if (seen.has(el)) continue;
                        seen.add(el);
                        const rect = el.getBoundingClientRect();
                        if (rect.width === 0 || rect.height === 0) continue;
                        const style = window.getComputedStyle(el);
                        if (style.display === 'none' || style.visibility === 'hidden') continue;
                        const titleEl = el.querySelector('.ui-dialog-title, .modal-title, .popup-title, h1, h2, h3, .k-window-title');
                        dialogs.push({
                          title: (titleEl ? titleEl.innerText : '').trim(),
                          body: el.innerText.substring(0, 1200),
                        });
                      }
                    }
                    return {
                      url: window.location.href,
                      // 8000 chars catches modals that render late in DOM order.
                      // The Order Request Preview page is ~3-4KB of text; this
                      // covers the whole page in practice.
                      bodyText: document.body.innerText.substring(0, 8000),
                      dialogs: dialogs,
                    };
                }"""
            )
        except Exception as e:
            logger.warning(f"_probe_submission_error: page.evaluate failed: {e}")
            return None

        dialogs = raw.get("dialogs") or []
        body_text: str = raw.get("bodyText") or ""

        # 1) Try each dialog, top to bottom, against each pattern
        for d in dialogs:
            dtitle = (d.get("title") or "").strip()
            dbody = (d.get("body") or "").strip()
            for pat in SUBMISSION_ERROR_PATTERNS:
                title_ok = bool(re.search(pat["title_regex"], dtitle, re.IGNORECASE))
                body_ok  = bool(re.search(pat["body_regex"],  dbody + "\n" + body_text, re.IGNORECASE))
                if title_ok or body_ok:
                    return {
                        "error_type": pat["error_type"],
                        "title": dtitle or pat["error_type"].replace("_", " ").title(),
                        "body_text": dbody[:800] or body_text[:800],
                        "prior_order_id": _first_prior_order(dbody + "\n" + body_text),
                        "matched_rule": pat["error_type"],
                        "cancel_button_text": pat.get("cancel_button_text"),
                    }

        # 2) No visible dialog matched — scan the whole page body
        for pat in SUBMISSION_ERROR_PATTERNS:
            if re.search(pat["body_regex"], body_text, re.IGNORECASE):
                return {
                    "error_type": pat["error_type"],
                    "title": pat["error_type"].replace("_", " ").title(),
                    "body_text": body_text[:800],
                    "prior_order_id": _first_prior_order(body_text),
                    "matched_rule": pat["error_type"] + "_body_only",
                    "cancel_button_text": pat.get("cancel_button_text"),
                }

        return None

    async def _cancel_submission(self, err: dict) -> None:
        """Back out of a submission-error modal without completing the submit.

        Tries in order:
          1. Click the modal's cancel/withdraw button by text (from the matched pattern).
          2. Click any visible .ui-dialog .ui-dialog-buttonpane button with 'cancel' text.
          3. Navigate to Carelon's homepage to free the session.

        Non-fatal — we always log and move on so the caller returns cleanly.
        """
        btn_text = err.get("cancel_button_text")
        try:
            if btn_text:
                # Try dialog-scoped first, then page-wide
                selectors = [
                    f".ui-dialog:not(.ui-dialog-hidden) button:has-text('{btn_text}')",
                    f"[role='dialog'] button:has-text('{btn_text}')",
                    f"button:has-text('{btn_text}')",
                    f"input[type='button'][value='{btn_text}']",
                ]
                for sel in selectors:
                    try:
                        locator = self.page.locator(sel).first
                        if await locator.count() > 0 and await locator.is_visible():
                            logger.info(f"_cancel_submission: clicking {btn_text!r} via {sel}")
                            await locator.click()
                            try:
                                await self.reader.wait_for_postback(timeout_ms=15000)
                            except Exception:
                                pass
                            return
                    except Exception:
                        continue
        except Exception as e:
            logger.warning(f"_cancel_submission: button-click path failed: {e}")

        # Fallback: navigate to homepage
        try:
            logger.info("_cancel_submission: falling back to homepage navigation")
            await self.page.goto("https://www.providerportal.com/Default.aspx", timeout=20000)
        except Exception as e:
            logger.warning(f"_cancel_submission: homepage navigation failed: {e}")

    async def download_auth_pdf(self) -> bytes | None:
        """Download the authorization PDF from the confirmation page.

        HAR-proven two-step flow:
          1. Click visible "Save as PDF" button (cmdSavePdf) → ASP.NET postback
          2. Server responds with same page + auto-click script that triggers
             hidden <a id="cmdSaveAsPdf" href="Download.aspx?enc=..."> in new tab
          3. New tab loads Download.aspx which serves the actual PDF

        We intercept step 2 by waiting for the new page (popup) that opens
        from the auto-click, then read the PDF bytes from that page's response.

        Returns:
            PDF bytes on success, None on failure.
        """
        import asyncio

        try:
            # Step 1: Click the visible "Save as PDF" submit button
            # This triggers a postback that generates the Download.aspx?enc= URL
            save_btn = await self.page.query_selector(SEL.get("save_pdf_button", "#asPrimary_ctl00_cmdSavePdf"))
            if not save_btn:
                save_btn = await self.page.query_selector("[id*='cmdSavePdf']")
            if not save_btn:
                logger.warning("No cmdSavePdf button found on confirmation page")
                return None

            logger.info("Clicking 'Save as PDF' button (postback)...")

            # The postback response includes an auto-click script that opens
            # Download.aspx in a new tab. We need to intercept that popup.
            # Use expect_page to catch the new tab opened by the auto-click.
            try:
                async with self.page.context.expect_page(timeout=30000) as popup_info:
                    await save_btn.click()
                    # Wait for postback to complete and auto-click script to fire
                    await self.reader.wait_for_postback(timeout_ms=25000)

                popup = await popup_info.value
                logger.info(f"PDF popup opened: {popup.url[:120]}")

                # Wait for the popup to load the PDF
                await popup.wait_for_load_state("load", timeout=15000)

                # Read the PDF bytes from the popup's response
                # Download.aspx serves the PDF directly
                pdf_bytes = await popup.evaluate("""
                    async () => {
                        try {
                            const resp = await fetch(window.location.href);
                            const buf = await resp.arrayBuffer();
                            return Array.from(new Uint8Array(buf));
                        } catch {
                            return null;
                        }
                    }
                """)

                if pdf_bytes:
                    pdf_data = bytes(pdf_bytes)
                    logger.info(f"Auth PDF downloaded via popup: {len(pdf_data)} bytes")
                    await popup.close()
                    return pdf_data if len(pdf_data) > 100 else None

                await popup.close()
                logger.warning("Could not read PDF bytes from popup")

            except Exception as popup_err:
                logger.info(f"Popup approach failed ({popup_err}), trying direct Download.aspx fetch...")

            # Fallback: Extract the Download.aspx?enc= URL from the page and fetch directly
            download_url = await self.page.evaluate("""
                () => {
                    const link = document.querySelector('[id*="cmdSaveAsPdf"]');
                    return link ? link.href : null;
                }
            """)

            if download_url:
                logger.info(f"Fetching PDF directly from: {download_url[:100]}...")
                # Use a new page to fetch the PDF (same session cookies)
                pdf_page = await self.page.context.new_page()
                try:
                    resp = await pdf_page.goto(download_url, wait_until="load", timeout=15000)
                    if resp and resp.ok:
                        pdf_bytes = await resp.body()
                        logger.info(f"Auth PDF downloaded via direct fetch: {len(pdf_bytes)} bytes")
                        return pdf_bytes if len(pdf_bytes) > 100 else None
                    else:
                        logger.warning(f"PDF fetch failed: status={resp.status if resp else 'no response'}")
                finally:
                    await pdf_page.close()
            else:
                logger.warning("No Download.aspx URL found in cmdSaveAsPdf link")

            return None

        except Exception as e:
            logger.warning(f"Failed to download auth PDF: {e}")
            return None
        finally:
            # Close any popup tabs that opened
            try:
                for p in self.page.context.pages:
                    if p != self.page:
                        await p.close()
            except Exception:
                pass
