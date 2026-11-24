"""Structural prose grammar: clause and paragraph pools per document role.

Every sentence here is a *structural element* — recitals, obligations, findings,
submissions — not free text (§3.4).  Each pool holds several interchangeable
phrasings, and the caller selects without replacement, so a document repeats a
shape but not a sentence.

Templates use ``{slot}`` fields filled from ``Matter.ctx``.  A missing slot
raises rather than rendering a placeholder, because a placeholder in a legal
document is a defect.
"""
from __future__ import annotations

from dataclasses import dataclass

from .rng import pick, shuffled

__all__ = ["fill", "Pool", "POOLS", "paragraphs", "connectors"]


def fill(template: str, ctx: dict[str, str]) -> str:
    try:
        text = template.format_map(_Strict(ctx))
    except KeyError as exc:
        raise KeyError(f"clause template needs slot {exc.args[0]!r}: {template[:70]!r}") from exc
    return " ".join(text.split())


class _Strict(dict):
    def __missing__(self, key):          # pragma: no cover - surfaces template bugs
        raise KeyError(key)


@dataclass(frozen=True)
class Pool:
    """A role's paragraph templates.  Each entry is one paragraph."""

    role: str
    templates: tuple[str, ...]

    def __len__(self) -> int:
        return len(self.templates)


POOLS: dict[str, Pool] = {}


def _pool(role: str, *templates: str) -> None:
    POOLS[role] = Pool(role=role, templates=tuple(templates))


# ---------------------------------------------------------------------------
# financial agreement
# ---------------------------------------------------------------------------
_pool(
    "fa.recital",
    "{holdco} is a company incorporated in {jurisdiction} and is the holding company of the group.",
    "The group carries on business in {jurisdiction} and in the other jurisdictions in which its subsidiaries operate.",
    "{lender} is a bank authorised to carry on regulated activities in {jurisdiction}.",
    "The Borrower has requested that the Lender make available to it a term facility of {principal} for the purposes described in this agreement.",
    "The Lender has agreed to make the facility available on the terms and subject to the conditions set out in this agreement.",
    "The obligations of the Borrower under this agreement are guaranteed by the Guarantors and secured by the Transaction Security.",
    "This agreement records the terms on which the facility is made available and supersedes any earlier term sheet between the parties.",
    "The parties have entered into this agreement in connection with the acquisition of {target} and the refinancing of the group's existing indebtedness.",
)

_pool(
    "fa.interpretation",
    "In this agreement, unless the context otherwise requires, words in the singular include the plural and words in the plural include the singular.",
    "A reference to a clause or a schedule is a reference to a clause of, or a schedule to, this agreement.",
    "A reference to a statute includes any subordinate legislation made under it and any amendment or re-enactment of it.",
    "Where an amount is expressed in a currency other than sterling, the Lender shall convert it at the rate prevailing on the relevant date.",
    "Any obligation of the Borrower to pay an amount is satisfied only when the amount is received in cleared funds by the Lender.",
    "Headings are for convenience only and do not affect the interpretation of this agreement.",
    "A period expressed in days runs from the day after the first event and includes the last day of the period.",
    "References to a document being in agreed form mean that it has been initialled by or on behalf of each party for identification.",
)

_pool(
    "fa.facility",
    "The Lender makes available to the Borrower a term loan facility in an aggregate amount of {principal}.",
    "The facility is available for drawing in up to four tranches during the availability period ending on {maturity}.",
    "The Borrower shall apply all amounts borrowed under the facility towards the acquisition of {target} and the payment of related fees and expenses.",
    "No amount borrowed under the facility may be applied towards any purpose other than those stated in clause 3.2 without the prior written consent of the Lender.",
    "The facility is a committed facility and, subject to the conditions precedent, the Lender is obliged to fund each valid drawdown request.",
    "The Borrower may not request a drawdown that would cause the aggregate amount outstanding to exceed {principal}.",
    "The availability period begins on the date of this agreement and ends on the date falling three months after the first drawdown.",
    "Amounts repaid or prepaid under the facility may not be re-borrowed.",
)

_pool(
    "fa.interest",
    "Interest accrues on each loan at the rate of {all_in}, being the margin of {margin} per annum over the reference rate of {rate}.",
    "Interest is calculated on the basis of the actual number of days elapsed and a year of 365 days.",
    "Interest accrues from the date of drawdown and is payable quarterly in arrear on each quarter date.",
    "If the Borrower fails to pay an amount when due, interest accrues on the overdue amount at the rate of {all_in} plus two per cent per annum.",
    "The Lender shall notify the Borrower of each determination of the reference rate as soon as reasonably practicable after it is made.",
    "Any interest that is not paid when due is capitalised and itself bears interest at the rate stated in clause 4.1.",
    "The margin may be adjusted by the Lender if the group's leverage ratio exceeds {leverage} for two consecutive quarters.",
    "A worked example of the interest calculation for the first quarter is set out in the schedule to this agreement.",
)

_pool(
    "fa.conditions",
    "The Lender is not obliged to make any loan available unless it has received all of the documents listed in schedule 2 in form and substance satisfactory to it.",
    "The conditions precedent must be satisfied on or before the date falling ten business days before the first drawdown.",
    "The Lender may waive any condition precedent in writing, and any waiver is effective only in respect of the drawdown to which it relates.",
    "If a condition precedent is not satisfied or waived by the required date, the facility is immediately cancelled.",
    "The Borrower shall deliver a drawdown request to the Lender not less than five business days before the proposed drawdown date.",
    "Each drawdown request is irrevocable once delivered and must specify the amount, the proposed date and the account to be credited.",
)

_pool(
    "fa.repayment",
    "The Borrower shall repay the loan in eight equal quarterly instalments, the first falling due on {first_payment}.",
    "Each instalment is applied first in payment of accrued interest and then in reduction of principal.",
    "The final instalment is due on {maturity}, at which date the facility must be repaid in full.",
    "The Borrower may prepay all or part of the loan on any quarter date on not less than ten business days' prior notice.",
    "Any prepayment is applied in inverse order of maturity and does not relieve the Borrower of its obligation to pay accrued interest.",
    "Amounts repaid under this clause are applied to the instalments specified in the repayment schedule.",
    "If the Borrower prepays the whole of the loan, it shall also pay any broken funding costs incurred by the Lender.",
    "The outstanding balance as at {as_at} is stated in clause 6.4 and must be confirmed by the Borrower in each compliance certificate.",
)

_pool(
    "fa.security",
    "The obligations of the Borrower are secured by a fixed and floating charge over the assets described in schedule 3.",
    "The Transaction Security includes a first-ranking legal charge over {asset1} and a fixed charge over the shares of each Guarantor.",
    "The Borrower shall ensure that the Transaction Security is registered at Companies House within 21 days of its creation.",
    "The Lender may enforce the Transaction Security if an event of default has occurred and is continuing.",
    "The Borrower shall not create or permit to subsist any security over its assets other than the Transaction Security and the Permitted Security.",
    "The aggregate value of the Charged Assets must at all times equal or exceed 125 per cent of the outstanding amount.",
)

_pool(
    "fa.guarantee",
    "Each Guarantor irrevocably and unconditionally guarantees to the Lender the punctual performance by the Borrower of all of its obligations under this agreement.",
    "The liability of a Guarantor under this clause is not discharged or affected by any amendment to this agreement or by any release of the Transaction Security.",
    "The Guarantors' obligations are joint and several and are secured in the manner described in clause 7.",
    "A Guarantor may not exercise any right of subrogation until the facility has been repaid in full.",
    "The Lender may make a demand on any Guarantor without first taking any step against the Borrower.",
)

_pool(
    "fa.undertaking",
    "The Borrower shall deliver to the Lender a compliance certificate signed by two directors within 30 days of each quarter end.",
    "The Borrower shall maintain insurance over the Charged Assets with a reputable insurer and shall note the Lender's interest on each policy.",
    "The Borrower shall not dispose of any material asset without the prior written consent of the Lender.",
    "The Borrower shall ensure that each member of the group complies with all laws and regulations applicable to its business.",
    "The Borrower shall notify the Lender promptly upon becoming aware of any event of default or any event that may become one.",
    "The Borrower shall procure that its subsidiaries do not incur any financial indebtedness other than Permitted Indebtedness.",
    "The Borrower shall use its reasonable endeavours to obtain and maintain all licences necessary for the operation of its business.",
    "The Borrower shall apply the proceeds of any insurance claim towards repair or replacement of the affected asset or prepayment of the facility.",
    "The Borrower shall not change its accounting reference date or its basis of accounting without the Lender's consent.",
    "The Borrower shall provide the Lender with such information about the group as the Lender may reasonably request.",
)

_pool(
    "fa.default",
    "Each of the following is an event of default: failure to pay, breach of a financial covenant, misrepresentation and insolvency.",
    "If an event of default occurs and is continuing, the Lender may by notice cancel the facility and declare the loan immediately due and payable.",
    "A failure to pay an amount when due is not an event of default unless it continues unremedied for three business days.",
    "The Borrower shall notify the Lender within two business days of becoming aware that an event of default has occurred.",
    "The Lender may exercise its rights under clause 9.2 without prejudice to any other right or remedy available to it.",
    "Any amendment to this agreement must be in writing and signed by or on behalf of each party.",
    "The Borrower may request an amendment to the financial covenants by delivering a revised business plan to the Lender.",
    "A waiver of any provision of this agreement is effective only if given in writing and only in the circumstances for which it is given.",
)

_pool(
    "fa.notice",
    "Any notice under this agreement must be in writing and delivered by hand, by pre-paid post or by email to the address specified in schedule 4.",
    "A notice delivered by hand is deemed received when left at the address, and a notice sent by email is deemed received on transmission.",
    "This agreement and any non-contractual obligations arising out of it are governed by the laws of {jurisdiction}.",
    "The courts of {jurisdiction} have exclusive jurisdiction to settle any dispute arising out of or in connection with this agreement.",
    "The parties have entered into this agreement as a deed on the date stated at the beginning of the document.",
)

_pool(
    "fa.definition_term",
    "Business Day means a day on which banks are open for general business in {jurisdiction}.",
    "Quarter Date means each date falling three, six, nine and twelve months after the date of this agreement.",
    "Financial Indebtedness includes any indebtedness for borrowed money and any amount raised under a finance lease.",
    "Permitted Security means any security arising by operation of law in the ordinary course of business.",
    "Permitted Indebtedness means indebtedness owed by a member of the group to another member of the group.",
    "Material Adverse Change means a change that materially and adversely affects the group's ability to perform its obligations.",
    "Reference Rate means the rate published by the relevant administrator for the currency of the loan.",
    "Transaction Security means the security created by the security documents listed in schedule 3.",
    "Compliance Certificate means a certificate substantially in the form set out in schedule 5.",
    "Leverage Ratio means consolidated net debt divided by consolidated EBITDA for the relevant period.",
)

_pool(
    "fa.representation",
    "The Borrower represents that it has full power and authority to enter into and perform its obligations under this agreement.",
    "The Borrower represents that the information it has supplied to the Lender in connection with the facility is true and accurate in all material respects.",
    "The Borrower represents that no event of default has occurred and is continuing as at the date of this agreement.",
    "The Borrower represents that it has good and marketable title to the Charged Assets free from any prior security.",
    "The Borrower represents that it is in compliance with all applicable licences and that no licence is subject to any material challenge.",
    "The Borrower represents that it has complied with the financial covenants throughout the period stated in the compliance certificate.",
)

# ---------------------------------------------------------------------------
# due diligence
# ---------------------------------------------------------------------------
_pool(
    "dd.scope",
    "This report records the limited financial and legal due diligence carried out on behalf of {lender} in connection with the proposed facility to {holdco}.",
    "The review covered the period from 1 January {period_start} to {period_end} and was based solely on the documents listed in appendix 1.",
    "The scope of the review was limited to the matters set out in the engagement letter and did not include an audit of the group's financial statements.",
    "We were not instructed to verify the accuracy of the management information supplied to us and express no opinion on it.",
    "Our findings are stated as at the date of this report and we have not updated them for events after that date.",
    "This report is prepared for the sole benefit of {lender} and may not be relied on by any other person.",
)

_pool(
    "dd.sources",
    "The documents reviewed included the statutory accounts, board minutes, the share register and the facility documentation for the existing borrowings.",
    "We also reviewed the group's management accounts for the last two financial years and the latest available management information.",
    "Legal proceedings were identified from the group's litigation register and from searches of the public court records.",
    "We received a certificate from the directors confirming that no material proceedings had been omitted from the litigation register.",
    "Where a document was provided in draft, we have assumed that the final version does not differ in any material respect.",
)

_pool(
    "dd.ownership",
    "The issued share capital of {holdco} is held by its shareholders in the proportions set out in the table below.",
    "The group structure comprises {holdco} and four operating subsidiaries, each wholly or majority owned by the holding company.",
    "The share register records no charge or encumbrance over the issued shares other than the Transaction Security.",
    "Each subsidiary is incorporated in {jurisdiction} and its shares are held in the percentages shown in the ownership schedule.",
    "We identified no unregistered shareholder agreement affecting the voting rights attaching to the issued shares.",
    "The group structure chart in appendix 2 reflects the position as at the date of our review.",
)

_pool(
    "dd.financial",
    "Group revenue for the year was {revenue_latest}, compared with {revenue_prior} in the prior year.",
    "EBITDA for the year was {ebitda_latest}, which represents a margin broadly in line with the prior year.",
    "Consolidated net debt as at the year end was {netdebt}, and the leverage ratio calculated on that basis was {leverage}.",
    "The management accounts for the current year show revenue running ahead of the same period last year.",
    "The group's cash conversion has been stable, and no significant working capital adjustment was identified.",
    "The financial information reviewed was prepared under the group's existing accounting policies and was not audited for the purposes of this report.",
)

_pool(
    "dd.proceedings",
    "The group is party to one material dispute, which is described in the chronology below.",
    "The claim is brought by {counterparty} and concerns the operation of the logistics contracts transferred as part of the acquisition.",
    "The group's solicitors consider that the claim is defensible and have not recommended a provision in the statutory accounts.",
    "A statutory demand was served on the group in connection with the dispute; the demand is disputed in its entirety.",
    "The court has ordered the parties to exchange witness statements before the next case management conference.",
    "We were unable to obtain a formal opinion from counsel and the exposure stated in this report is an estimate only.",
)

_pool(
    "dd.appendix",
    "Appendix 1 lists the documents provided to us and the date on which each was received.",
    "Appendix 2 contains the group structure chart and the shareholding percentages as recorded in the register.",
    "Appendix 3 sets out the management information relied on for the financial summary in this report.",
    "The appendices form part of this report and should be read together with the findings above.",
)

_pool(
    "dd.limitation",
    "The review did not extend to tax, pensions, environmental or employment matters, which were the subject of separate reports.",
    "We have relied on the accuracy of the certificates and management representations provided to us.",
    "No investigation was carried out into the group's intellectual property portfolio beyond the documents listed in appendix 1.",
    "Our work has been limited to the documents listed in appendix 1; no site visits, management interviews or third-party enquiries were undertaken.",
    "We have not been instructed to advise on the tax consequences of the proposed transaction and express no view on them.",
    "Any financial information summarised in this report has been extracted from the group's management accounts without independent verification.",
    "This report is addressed to {lender} and is provided solely for the purposes of the proposed facility.",
)

# ---------------------------------------------------------------------------
# board minutes
# ---------------------------------------------------------------------------
_pool(
    "bm.opening",
    "The meeting of the board of directors of {holdco} was held on {meeting_date} at the registered office.",
    "The chair welcomed the directors and confirmed that notice of the meeting had been given to all directors entitled to receive it.",
    "The chair noted that the meeting had been convened to consider the matters set out in the agenda circulated with the notice.",
    "The company secretary confirmed that a quorum was present and that the meeting was duly constituted.",
    "The chair declared the meeting open and the minutes of the previous meeting were considered.",
)

_pool(
    "bm.quorum",
    "A quorum was present throughout the meeting and no director declared an interest in any item of business other than as recorded below.",
    "The chair confirmed that the directors present constituted a quorum in accordance with the articles of association.",
    "The company secretary recorded the attendance and the apologies received from directors unable to attend.",
    "The board noted that a director who had declared an interest in the facility had withdrawn from the discussion and the vote.",
)

_pool(
    "bm.resolution",
    "The board considered the paper circulated in advance of the meeting and resolved as recorded in the resolutions table.",
    "After discussion, the resolution was put to the meeting and carried by the required majority.",
    "The chair noted that the resolution had been proposed by {director1} and seconded by {director2}.",
    "The board resolved that the actions recorded in the minutes be delegated to the company secretary for implementation.",
    "The directors discussed the commercial risks of the proposal and received an update from the lender's advisers.",
    "The board noted that the resolution would take effect from the date stated in the resolutions table.",
)

_pool(
    "bm.decision",
    "The board approved the facility agreement with {lender} in the amount of {principal} and authorised the directors to execute the related documents.",
    "The board noted the draft facility agreement and approved its terms subject to the completion of the conditions precedent.",
    "The board considered the proposed disposal and resolved that it was not in the best interests of the company at the present time.",
    "The board deferred consideration of the treasury policy pending further advice from the auditors.",
    "The board approved the updated risk register and asked management to report on the principal risks at each meeting.",
    "The board noted the group's performance against budget and approved the revised forecast for the remaining quarters.",
)

_pool(
    "bm.director",
    "The board noted the appointment of {director1} as a director of the company with effect from the date stated in the resolutions.",
    "The board accepted the resignation of {director2} as a director and recorded its thanks for her service to the company.",
    "The board approved the appointment of {chair} as chair of the audit committee for a period of three years.",
    "The board confirmed that the company secretary would file the required forms at Companies House within the statutory period.",
    "The board considered the results of the board evaluation and agreed an action plan for the coming year.",
)

_pool(
    "bm.prior",
    "The minutes of the meeting held on {prior_meeting} were approved as a correct record and signed by the chair.",
    "The board reviewed the actions arising from the previous meeting and noted that all but one had been completed.",
    "The chair reported on the matters discussed at the last meeting of the audit committee.",
    "The board noted the update from the remuneration committee and approved the recommended changes to the bonus framework.",
)

_pool(
    "bm.schedule",
    "The board received and noted the supporting schedule circulated with the papers.",
    "The schedule sets out the drawdown timetable, the amounts requested and the accounts to be credited.",
    "The board asked that the schedule be updated before the next drawdown request is submitted to the lender.",
)

_pool(
    "bm.closing",
    "There being no further business, the chair declared the meeting closed.",
    "The next meeting of the board was scheduled for the date recorded in the company's meetings calendar.",
    "The chair thanked the directors for their attendance and closed the meeting at the time recorded in the attendance sheet.",
)

# ---------------------------------------------------------------------------
# case notes
# ---------------------------------------------------------------------------
_pool(
    "cn.matter",
    "This note records the procedural position in the proceedings brought by {counterparty} against {holdco}.",
    "The claim is proceeding in {court} under claim number {case_number} before {judge}.",
    "The note is prepared for the purpose of the case management file and records the position as at the date stated below.",
    "The parties have exchanged statements of case and the matter is now listed for a case management conference.",
)

_pool(
    "cn.parties",
    "The claimant is represented by {counsel}, and the defendant is represented by {counsel2}.",
    "The defendant's solicitors have confirmed that they hold instructions to act in the proceedings.",
    "The court has granted permission for a representative of the group's insurer to attend the hearing.",
    "The parties have agreed a timetable for disclosure and expert evidence, subject to the approval of the court.",
)

_pool(
    "cn.chronology",
    "The chronology below records the material events in the proceedings and the documents from which each is taken.",
    "The events are listed in date order and references are to the bundle prepared for the case management conference.",
    "The claimant served the claim form and particulars of claim on the dates shown in the chronology.",
    "The defence was filed within the extended time allowed by the court's order.",
    "The parties attended a without-prejudice settlement meeting, which did not result in agreement.",
)

_pool(
    "cn.evidence",
    "The evidence register lists the documents relied on by each party and the status of each document.",
    "The claimant relies on the correspondence exchanged during the tender process and on the witness evidence of two former employees.",
    "The defendant objects to the admissibility of the internal report on the grounds of legal professional privilege.",
    "The court has not yet ruled on the privilege objection, and the document remains excluded from the bundle pending determination.",
    "The expert evidence is to be exchanged in accordance with the timetable approved by the court.",
)

_pool(
    "cn.submission",
    "The claimant submits that the contract was varied by conduct and that the defendant's refusal to perform amounted to a repudiatory breach.",
    "The defendant submits that the alleged variation was not supported by consideration and is therefore unenforceable.",
    "The defendant further submits that the claim is time-barred under the applicable limitation period.",
    "The claimant responds that the limitation period began to run only when the breach was discovered.",
    "The court has indicated that it will determine the limitation issue as a preliminary point.",
)

_pool(
    "cn.procedural",
    "The proceedings have moved from the pleadings stage to case management, and the next hearing is listed for the date shown below.",
    "The court extended the time for disclosure by 28 days on the application of the defendant.",
    "The parties have been directed to consider mediation and to report the outcome to the court.",
    "The claim has been transferred to the commercial list and allocated to a new judge.",
    "The court stayed the proceedings pending the outcome of the related arbitration.",
)

_pool(
    "cn.order",
    "The court ordered that the parties exchange witness statements by the date stated in the order.",
    "The court ordered that the defendant file and serve a defence to the counterclaim within 21 days.",
    "The court ordered that costs of the application be reserved to the trial judge.",
    "The court directed that the parties provide a joint bundle and an agreed chronology before the next hearing.",
    "The court ordered the defendant to notify the claimant of any change of address within seven days.",
)

_pool(
    "cn.next",
    "The next step is for the parties to agree the terms of a consent order and submit it for approval.",
    "The defendant should consider whether to make an application for summary judgment before the next hearing.",
    "The claimant should obtain a supplementary witness statement addressing the documents disclosed by the defendant.",
    "The parties should report the outcome of mediation to the court by the date specified in the order.",
)

# ---------------------------------------------------------------------------
# memorandum
# ---------------------------------------------------------------------------
_pool(
    "mm.summary",
    "This memorandum summarises our advice on the facility and the security package proposed by {lender}.",
    "In our view the facility is enforceable in accordance with its terms, subject to the qualifications set out below.",
    "The principal conclusions are that the guarantee is valid, that the security is capable of registration and that the conditions precedent are capable of satisfaction.",
    "We have assumed that the documents reviewed are complete and that no side letter alters the terms of the facility.",
    "Our conclusions are qualified by the matters set out in section 7 of this memorandum.",
)

_pool(
    "mm.background",
    "The group has agreed terms with {lender} for a term facility of {principal} to finance the acquisition of {target}.",
    "The facility is guaranteed by the Guarantors and secured over the assets described in the security documents.",
    "The group's existing indebtedness is to be refinanced from the proceeds of the first drawdown.",
    "The transaction completed on {acquisition_date}, and the facility was made available shortly afterwards.",
    "The directors have approved the transaction and the shareholders have passed the required resolutions.",
)

_pool(
    "mm.analysis",
    "The first issue is whether the guarantee is enforceable against each Guarantor given the corporate benefit received by each of them.",
    "In our view there is sufficient corporate benefit to support the guarantee, and the directors have recorded that conclusion in the board minutes.",
    "The second issue is whether the security created over the shares is registrable, and we consider that it is.",
    "The third issue concerns the financial covenants, which are expressed by reference to consolidated EBITDA and net debt.",
    "The definitions of EBITDA and net debt should be reviewed against the group's accounting policies before the first certificate is delivered.",
    "The fourth issue is the scope of the representations, which are given on the date of each drawdown request rather than only at signing.",
    "We consider that the representations are appropriately limited and that the material adverse change provision is within market norms.",
    "The fifth issue concerns the enforcement regime, which permits the lender to accelerate on a change of control.",
)

_pool(
    "mm.relationships",
    "Each member of the group owes obligations to the lender under the facility, and the Guarantors' liability is joint and several.",
    "The intra-group loans are subordinated to the facility and may not be repaid while an event of default is continuing.",
    "The lender's rights are supported by a first-ranking charge over the charged assets and by the share charges.",
    "The parent company has undertaken not to dispose of its shares in the operating subsidiaries without consent.",
)

_pool(
    "mm.exceptions",
    "Our conclusions are subject to the assumption that the borrower was solvent at the time the security was granted.",
    "The advice does not address the tax treatment of the facility or the position of any shareholder who is not a party to it.",
    "If the court were to find that the guarantee was given without corporate benefit, the guarantee may be set aside.",
    "We have not reviewed the financial models supporting the covenant headroom and express no view on their assumptions.",
    "The advice is given on the basis of the law in force at the date of this memorandum and does not cover subsequent changes.",
)

_pool(
    "mm.exhibit",
    "The documents referred to in this memorandum are listed in the schedule of exhibits.",
    "Copies of the facility agreement and the security documents are attached as exhibits 1 and 2.",
    "The board minutes approving the transaction are exhibited for completeness.",
)

# ---------------------------------------------------------------------------
# press release
# ---------------------------------------------------------------------------
_pool(
    "pr.headline",
    "{holdco} completes the acquisition of {target}",
    "{holdco} announces new facility with {lender}",
    "{holdco} reports full-year results",
    "{holdco} appoints new chair of the audit committee",
)

_pool(
    "pr.announcement",
    "{holdco}, the {jurisdiction}-based {sector} group, today announces that it has completed the acquisition of {target} for a consideration of {acquisition}.",
    "The acquisition was financed from a new term facility of {principal} provided by {lender}.",
    "The transaction was approved by the boards of both companies and completed on {acquisition_date}.",
    "{target} will continue to operate as a separate brand within the group and its management team will remain in place.",
    "The group expects the acquisition to be earnings enhancing in the first full year of ownership.",
    "The facility has a tenor of {tenor_years} years and is repayable in quarterly instalments.",
)

_pool(
    "pr.status",
    "The acquisition is unconditional, and all regulatory clearances required for completion have been obtained.",
    "The facility is subject to the satisfaction of customary conditions precedent, which the group expects to satisfy within the next quarter.",
    "Trading in the year to date has been in line with the board's expectations.",
    "The integration of the acquired business will be led by the group's existing operating committee.",
    "The group's order book at the year end was higher than at the same point last year.",
)

_pool(
    "pr.historical",
    "Group revenue for the prior year was {revenue_prior}, and the group reported EBITDA of {ebitda_prior}.",
    "The results compare with revenue of {revenue_prior} in the previous financial year.",
    "Net debt at the year end was {netdebt}, and the group continues to operate within its covenant headroom.",
    "The group has delivered revenue growth in each of the last three financial years.",
)

_pool(
    "pr.boilerplate",
    "About {holdco}: the group is a {sector} business employing approximately 1,400 people across four countries.",
    "{holdco} is incorporated in {jurisdiction} and its shares are privately held.",
    "The group operates through four trading subsidiaries and a shared services centre.",
    "For further information, please contact the company secretary at the registered office.",
)

_pool(
    "pr.contact",
    "Media enquiries should be directed to the company secretary, {secretary}.",
    "This announcement contains forward-looking statements that are subject to risks and uncertainties.",
    "Nothing in this announcement constitutes an offer of securities or a profit forecast.",
)

# ---------------------------------------------------------------------------
# correspondence
# ---------------------------------------------------------------------------
_pool(
    "co.opening",
    "We write on behalf of {holdco} in relation to the facility agreement dated {start_date} between the company and {lender}.",
    "We refer to your letter of {as_at} and to the facility agreement dated {start_date}.",
    "Further to our recent correspondence, we set out below the company's position on the outstanding matters.",
    "We acknowledge receipt of your letter and respond as follows.",
    "We are instructed to write to you concerning the compliance certificate for the quarter ended {as_at}.",
    "Thank you for your letter of {as_at}, to which we reply in this letter.",
)

_pool(
    "co.position",
    "The company does not accept that an event of default has occurred under facility {facility_ref}, and it reserves all of its rights.",
    "The amounts due under facility {facility_ref} have been paid in accordance with the repayment schedule; the balance is {outstanding}.",
    "The company has complied with the covenants under facility {facility_ref} and delivered the certificate on time.",
    "The company disputes the leverage calculation under facility {facility_ref} and has asked the auditors to review it.",
    "The company has provided the information requested under facility {facility_ref} and will continue to cooperate.",
    "The company considers that the notice under facility {facility_ref} does not comply with clause 20.",
    "The company's position on facility {facility_ref} is supported by the advice of {counsel}, enclosed with this letter.",
    "The company has maintained headroom under facility {facility_ref} throughout the period.",
)

_pool(
    "co.request",
    "We request that the lender withdraw the notice under facility {facility_ref} and confirm that no event of default is continuing.",
    "We request a meeting with the lender's advisers to discuss the covenant calculation under facility {facility_ref}.",
    "We ask the lender to confirm that it will not take enforcement action under facility {facility_ref} while the parties discuss an amendment.",
    "We request copies of the documents on which the lender relies in support of its position under facility {facility_ref}.",
    "We propose a short standstill agreement under facility {facility_ref} on the terms set out in the enclosed draft.",
    "We would be grateful for a response on facility {facility_ref} within ten business days of the date of this letter.",
    "We request a waiver of the covenant tested at {as_at} under facility {facility_ref}.",
    "We ask the lender to confirm the outstanding balance under facility {facility_ref} as at {as_at}.",
    "We request an extension of the delivery date for the compliance certificate under facility {facility_ref}.",
    "We propose that the parties meet on {as_at} to discuss facility {facility_ref}.",
    "We request that any further correspondence on facility {facility_ref} be directed to the undersigned.",
    "We reserve the right to refer the matter to the lender's regulator if facility {facility_ref} is not amended.",
)

_pool(
    "co.thread",
    "This letter responds to your letter of {parent_date} and should be read together with it.",
    "We refer to our earlier correspondence in this matter, copies of which are enclosed for completeness.",
    "Our previous letter of {parent_date} set out the company's position, which we maintain.",
    "This letter supersedes our letter of {parent_date} to the extent of any inconsistency.",
    "We note the points raised in your letter and address each of them below.",
)

_pool(
    "co.attachment",
    "The following documents are enclosed with this letter: the compliance certificate, the covenant calculation and the board minute approving the calculation.",
    "A copy of the auditors' review of the covenant calculation is attached as an exhibit.",
    "The enclosed schedule sets out the payments made under the facility during the quarter.",
)

_pool(
    "co.correction",
    "We write to correct an error in our letter of {parent_date}: the outstanding balance was stated as {outstanding}, and the correct figure is {outstanding_correct}.",
    "Our earlier letter contained an arithmetical error in the covenant calculation, which we correct in the enclosed schedule.",
    "We withdraw the statement in our letter of {parent_date} that the company had received the notice on an earlier date.",
)

_pool(
    "co.closing",
    "We look forward to your response on facility {facility_ref} and remain available to discuss any aspect of this letter.",
    "This letter is sent without prejudice to the company's rights under facility {facility_ref}, all of which are reserved.",
    "Please direct any correspondence on facility {facility_ref} to the undersigned.",
    "We will write again on facility {facility_ref} once the auditors have completed their review.",
    "Nothing in this letter constitutes a waiver of the company's rights under facility {facility_ref}.",
    "We remain available to discuss facility {facility_ref} at the lender's convenience.",
)


# ---------------------------------------------------------------------------
# assembly helpers
# ---------------------------------------------------------------------------
def paragraphs(role: str, ctx: dict[str, str], rng, count: int,
               *, exclude: set[str] | None = None,
               used: set[str] | None = None) -> list[str]:
    """Fill ``count`` distinct templates from one role, without replacement.

    ``used`` is the document-wide sentence registry.  The first pass refuses any
    text already written in the document; only if the pool cannot satisfy the
    request is ``used`` relaxed for texts that are still new *within this call*.
    A pool this small is a grammar bug, but the caller must not hang or raise.
    """
    pool = POOLS.get(role)
    if pool is None:
        raise KeyError(f"unknown prose role {role!r}")
    templates = [t for t in pool.templates if t not in (exclude or set())]
    if not templates:
        raise KeyError(f"role {role!r} has no templates left")
    out: list[str] = []
    blocked: set[str] = set()
    # pass 1: nothing already in the document; pass 2: only avoid in-call repeats
    for strict in (True, False):
        if len(out) >= count:
            break
        for _ in range(4):
            batch = shuffled(rng, templates)
            added = 0
            for template in batch:
                if len(out) >= count:
                    break
                text = fill(template, ctx)
                if text in out:
                    continue
                if strict and text in (used or set()):
                    continue
                out.append(text)
                added += 1
            if not added or len(out) >= count:
                break
    if len(out) < count:
        raise RuntimeError(f"could only fill {len(out)} of {count} sentences for {role!r}")
    if used is not None:
        used.update(out)
    return out


# ---------------------------------------------------------------------------
# compositional sentences
# ---------------------------------------------------------------------------
# Hand-authoring every sentence would either cap depth or produce padding.  Each
# generator below combines a subject, a predicate and a qualifier, so a small
# authored inventory yields a large, meaningful sentence space (§10.2 axis
# "realisation").  Combinations are sampled without repetition.

OBLIGATION_SUBJECTS = (
    "The Borrower", "Each Obligor", "The Guarantor", "Each group member",
    "{holdco}", "{guarantor}",
)
OBLIGATION_PREDICATES = (
    "deliver a compliance certificate",
    "maintain insurance over the Charged Assets",
    "procure group compliance with the treasury policy",
    "provide quarterly management accounts",
    "keep the Transaction Security first-ranking",
    "notify the Lender of material litigation",
    "apply disposal proceeds to prepayment",
    "keep proper books of account",
    "comply with the financial covenants",
    "obtain consent for further indebtedness",
    "keep the Charged Assets in good repair",
    "forward notices from any authority",
    "maintain its registration and good standing",
    "provide an auditors' leverage certificate",
    "grant no security beyond Permitted Security",
    "pay without set-off or counterclaim",
    "notify changes in senior management",
    "use reasonable endeavours to obtain consents",
    "keep the guarantee in full force",
    "provide further information on request",
)
QUALIFIERS = (
    "within five Business Days",
    "in a form acceptable to the Lender",
    "at all times",
    "promptly on request",
    "unless the Lender agrees otherwise",
    "in accordance with schedule 4",
    "to the extent permitted by law",
    "before the first drawdown",
    "on each quarter date",
    "throughout the facility period",
    "without prejudice to its rights",
    "within 30 days",
)
OBLIGATION_FRAMES = (
    "{s} shall {p} {q}.",
    "{s} must {p} {q}.",
    "{s} is required to {p} {q}.",
    "It is a condition that {s} {p} {q}.",
    "{s} shall {p}.",
    "{s} must {p}.",
    "{s} shall {p}.",
    "{s} must {p}.",
    "{s} shall {p}.",
)

CLAUSE_SUBJECTS = (
    "The Lender", "The Borrower", "Any Guarantor", "The parties", "Each Obligor",
)
CLAUSE_PREDICATES = (
    "may require prepayment",
    "may cancel the facility by notice",
    "may assign its rights",
    "may exercise a right of set-off",
    "need not fund while a default continues",
    "may disclose information to its advisers",
    "shall give ten Business Days' notice",
    "may waive any provision in writing",
    "may rely on a director's certificate",
    "may appoint a receiver",
    "is not liable for delay",
    "may require an updated structure chart",
)
CLAUSE_QUALIFIERS = (
    "on the terms of this agreement",
    "without prejudice to its other rights",
    "subject to clause 22",
    "in accordance with schedule 6",
    "after consulting the Borrower",
    "while the facility remains available",
    "unless the parties agree otherwise",
    "to protect its position",
)
CLAUSE_FRAMES = (
    "{s} {p} {q}.",
    "{s} {p}, {q}.",
    "For the avoidance of doubt, {s} {p} {q}.",
    "{s} {p}.",
    "{s} {p}.",
    "{s} {p}.",
    "{s} {p}.",
)

REVIEW_SUBJECTS = (
    "We", "The review", "Our review", "The diligence team", "The advisers",
)
REVIEW_PREDICATES = (
    "reviewed the statutory accounts",
    "tested the covenant arithmetic",
    "reconciled the share register",
    "obtained written representations",
    "compared the forecast with the budget",
    "reviewed the security documents",
    "checked the conditions precedent",
    "considered the regulator's correspondence",
    "reviewed the related-party balances",
    "assessed the asset valuation",
    "traced the drawdown proceeds",
    "reviewed the board minutes",
)
REVIEW_QUALIFIERS = (
    "for the period covered",
    "on the documents in appendix 1",
    "without an audit",
    "against management's information",
    "using the stated policies",
    "with the finance team",
    "subject to the stated limitations",
    "within the agreed scope",
)
REVIEW_FRAMES = (
    "{s} {p} {q}.",
    "{s} {p} {q}, and no exception was noted.",
    "As part of the review, {s} {p} {q}.",
    "{s} {p} {q}; the results appear below.",
    "{s} {p}.",
    "{s} {p}.",
    "{s} {p}.",
)

FINDING_SUBJECTS = (
    "We", "The review", "Our review", "The court", "The advisers",
)
FINDING_PREDICATES = (
    "found no material misstatement",
    "found the disclosures consistent",
    "concluded that the allegations were unsupported",
    "noted the related-party disclosure",
    "identified a covenant discrepancy",
    "found the security duly registered",
    "concluded that the structure chart was accurate",
    "noted one omitted dormant claim",
    "found no undisclosed guarantee",
    "concluded that the valuation was unsupported",
)
FINDING_QUALIFIERS = (
    "on the documents provided",
    "subject to the limitations above",
    "as at the report date",
    "after enquiries of management",
    "on the underlying records",
    "to the extent information was available",
)
FINDING_FRAMES = (
    "{s} {p} {q}.",
    "{s} {p} {q}, as recorded in section 9.",
    "In particular, {s} {p} {q}.",
    "{s} {p} {q}; follow-up is recommended.",
    "{s} {p}.",
    "{s} {p}.",
    "{s} {p}.",
)

BOARD_SUBJECTS = ("The board", "The directors", "The chair", "The committee")
BOARD_PREDICATES = (
    "considered the circulated paper",
    "discussed the commercial risks",
    "received a presentation from the advisers",
    "noted performance against budget",
    "approved the revised forecast",
    "considered the draft minutes",
    "reviewed the risk register",
    "noted the audit committee update",
    "approved the auditors' appointment",
    "considered the proposed dividend",
    "noted the facility position",
    "approved the related-party loan",
)
BOARD_QUALIFIERS = (
    "after a full discussion",
    "in the light of the advisers' report",
    "subject to the conditions precedent",
    "before the next meeting",
    "on the committee's recommendation",
    "without dissent",
    "with one director abstaining",
    "after the declared interest",
)
BOARD_FRAMES = (
    "{s} {p} {q}.",
    "{s} {p} {q}, and the chair recorded the decision.",
    "After discussion, {s} {p} {q}.",
    "{s} {p} {q}; management will report back.",
    "{s} {p}.",
    "{s} {p}.",
    "{s} {p}.",
)

PROCEDURE_SUBJECTS = ("The court", "The claimant", "The defendant", "The parties")
PROCEDURE_PREDICATES = (
    "served the claim form",
    "filed a defence",
    "applied for more time",
    "agreed a witness timetable",
    "attended a case management conference",
    "requested further particulars",
    "objected to the internal report",
    "applied for summary judgment",
    "agreed to mediate",
    "served a notice to admit",
    "filed a reply",
    "applied for costs",
)
PROCEDURE_QUALIFIERS = (
    "within the time allowed",
    "on the recorded date",
    "under the procedural rules",
    "without prejudice to its position",
    "subject to the court's approval",
    "after the directions hearing",
    "before the next listing",
    "at the case conference",
)
PROCEDURE_FRAMES = (
    "{s} {p} {q}.",
    "{s} {p} {q}, as the chronology records.",
    "On the date shown, {s} {p} {q}.",
    "The file records that {s} {p} {q}.",
    "{s} {p}.",
    "{s} {p}.",
    "{s} {p}.",
)

SUBMISSION_SUBJECTS = ("The claimant", "The defendant", "The claimant's counsel",
                      "The defendant's counsel")
SUBMISSION_PREDICATES = (
    "submits that the contract was varied",
    "submits that the claim is time-barred",
    "argues that the exclusion clause fails",
    "contends that the refusal was repudiatory",
    "submits that there was no consideration",
    "argues that the report is privileged",
    "contends that the loss is too remote",
    "submits that time ran from discovery",
    "argues that the counterclaim is abusive",
    "contends that the parties intended a binding contract",
)
SUBMISSION_QUALIFIERS = (
    "on the cited authorities",
    "in its written submissions",
    "at the hearing",
    "by reference to the disclosed documents",
    "in the alternative",
    "as the note of the hearing records",
)
SUBMISSION_FRAMES = (
    "{s} {p} {q}.",
    "{s} {p} {q}, and invites a preliminary ruling.",
    "In argument, {s} {p} {q}.",
    "{s} {p} {q}; the other side disputes that.",
    "{s} {p}.",
    "{s} {p}.",
    "{s} {p}.",
)

ANALYSIS_SUBJECTS = ("We", "In our view", "The analysis", "The position")
ANALYSIS_PREDICATES = (
    "consider the guarantee enforceable",
    "consider the security registrable",
    "consider the covenants within market norms",
    "identify a change-of-control risk",
    "consider the conditions capable of satisfaction",
    "consider the representations appropriately limited",
    "note that EBITDA needs adjustment",
    "consider the intra-group loans subordinated",
    "identify tension with the disposal covenant",
    "consider the amendment mechanism workable",
)
ANALYSIS_QUALIFIERS = (
    "on the documents reviewed",
    "subject to the qualifications in section 6",
    "having regard to the commercial purpose",
    "in the light of the lender's policy",
    "unless the parties agree otherwise",
    "assuming continued solvency",
)
ANALYSIS_FRAMES = (
    "{s} {p} {q}.",
    "{s} {p} {q}, and recommend a side letter.",
    "For these reasons, {s} {p} {q}.",
    "{s} {p} {q}; the risk is manageable.",
    "{s} {p}.",
    "{s} {p}.",
    "{s} {p}.",
)

MEDIA_SUBJECTS = ("The group", "The company", "Management", "The board")
MEDIA_PREDICATES = (
    "has completed the acquisition",
    "has agreed a new facility",
    "reports revenue growth",
    "has appointed a new audit chair",
    "continues to invest in its platform",
    "has expanded into two markets",
    "has refinanced its borrowings",
)
MEDIA_QUALIFIERS = (
    "as planned",
    "this quarter",
    "while controlling costs",
    "in line with its strategy",
    "with further progress expected",
)
MEDIA_FRAMES = (
    "{s} {p} {q}.",
    "Today, {s} {p} {q}.",
    "{s} {p} {q}, the board confirmed.",
    "{s} {p}.",
    "{s} {p}.",
    "{s} {p}.",
)

LETTER_SUBJECTS = ("We", "The company", "Our client", "The parties")
LETTER_PREDICATES = (
    "do not accept that a default has occurred",
    "have complied with the covenants",
    "dispute the leverage calculation",
    "have delivered the compliance certificate",
    "consider the notice defective",
    "have provided the requested information",
    "reserve the company's rights",
    "have instructed the auditors to review",
)
LETTER_QUALIFIERS = (
    "for the reasons below",
    "in response to your letter",
    "on the enclosed calculation",
    "without prejudice to its position",
    "as the schedule explains",
    "pending the parties' discussions",
)
LETTER_FRAMES = (
    "{s} {p} {q}.",
    "{s} {p} {q}, and ask you to confirm in writing.",
    "For the avoidance of doubt, {s} {p} {q}.",
    "{s} {p} {q}; this letter responds to yours.",
    "{s} {p}.",
    "{s} {p}.",
    "{s} {p}.",
)


def compose(rng, ctx: dict[str, str], *, subjects, predicates, qualifiers, frames,
            count: int, used: set[str] | None = None) -> list[str]:
    """Sample distinct subject/predicate/qualifier combinations, filled."""
    seen: set[str] = set()
    out: list[str] = []
    attempts = 0
    while len(out) < count and attempts < count * 120:
        attempts += 1
        subject = fill(pick(rng, subjects), ctx)
        predicate = fill(pick(rng, predicates), ctx)
        qualifier = fill(pick(rng, qualifiers), ctx)
        text = fill(pick(rng, frames), {"s": subject, "p": predicate, "q": qualifier})
        if text in seen or (used is not None and text in used):
            continue
        seen.add(text)
        out.append(text)
    if len(out) < count and used:
        # relax the document registry before giving up: still no in-call repeats
        while len(out) < count and attempts < count * 240:
            attempts += 1
            subject = fill(pick(rng, subjects), ctx)
            predicate = fill(pick(rng, predicates), ctx)
            qualifier = fill(pick(rng, qualifiers), ctx)
            text = fill(pick(rng, frames), {"s": subject, "p": predicate, "q": qualifier})
            if text in seen:
                continue
            seen.add(text)
            out.append(text)
    if len(out) < count:  # pragma: no cover - inventory is far larger than any doc
        raise RuntimeError(f"could only compose {len(out)} of {count} sentences")
    if used is not None:
        used.update(out)
    return out


def connectors(rng) -> str:
    """A short discourse marker; used sparingly so prose does not read as a list."""
    return pick(rng, ("", "", "", "In addition, ", "Accordingly, ", "For the avoidance of doubt, "))
