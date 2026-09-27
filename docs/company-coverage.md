# Company coverage

The tracking list targets potential roles for a spring-2026 MS CS graduate in the
US, Canada, Australia and India. Employer selection covers scientific software,
healthcare, aviation, industrial systems, financial services, regional business
software and infrastructure. A company's recruiting platform does not determine
whether it belongs in the list.

## Enabled employers

The 300 enabled employers are defined in [the configuration](../config/companies.yaml).
The latest source check is in [the validation report](company-validation.json).
Counts in that report are a dated observation, not a promise that a position remains
open or that its requirements fit the candidate. The report predates the US tech
expansion below; its config hash identifies the 67-employer list it checked.

### Scientific computing, simulation and healthcare

MathWorks, COMSOL, Wolfram, Kitware, Schrödinger, Esri, SAS, Flatiron Health, PathAI,
Natera, Recursion, Benchling and PointClickCare.

### Aviation, robotics, industrial technology and energy

Flexjet, Garmin, Zipline, Motional, Nuro, Torc Robotics, Waabi, Skydio, Samsara,
Redwood Materials, Gecko Robotics, Form Energy, Geotab and LHP Engineering Solutions.

### Financial technology and business software

Continental Finance, Bloomberg, Enova, Blend, Upstart, Alarm.com, Appian, Guidewire,
Razorpay, Harness, Wealthsimple, Zensurance, Jane, Xero, Culture Amp, Faire, DaySmart,
ChurnZero, Identity Digital, Kinaxis and D2L.

### Data infrastructure, cloud, security and developer tools

Cockroach Labs, SingleStore, Yugabyte, Grafana Labs, Imply, Fivetran, Auvik, Rubrik,
Druva, Elastic, ClickHouse, BrowserStack, Cohere, NVIDIA, Ashby, Ramp, Sentry, Discord
and Palantir.

## US tech expansion (September 2026)

233 employers added on 2026-09-27 to widen US coverage. Candidates came from
employers posting US software, data, hardware, product and quant roles in the
SimplifyJobs new-grad dataset, on boards Role Radar can read. Each was kept only if
a live `role-radar list-matches` run with the default filters found at least five
matching US openings, and its matches were mostly software, data or IT roles rather
than civil, mechanical or HVAC engineering titles. Universities, state agencies,
staffing firms and small cleared-contract shops were left out. Many defense and
aerospace roles require US citizenship or a clearance.

### AI labs and AI-native startups

Anthropic, Cerebras, Crusoe, Cursor, Etched, Field AI, Glean, Graphcore, Harvey, Lambda,
NewsBreak, OpenAI, Perplexity, Replit, Scale AI, Sierra AI, Solace Health, Tenstorrent,
Together AI, Truveta and xAI.

### Big tech, cloud, enterprise software and media

Adobe, Alkami Technology, Asana, Asure, Autodesk, Axon, Blue Yonder, Broadcom, Ciena,
Cisco, Clearwater Analytics, CoStar Group, Cox Enterprises, CrowdStrike, Databricks,
Datadog, Disney, eBay, Expedia, F5, Figma, Gartner, Hewlett Packard Enterprise, HP,
iHeartMedia, LexisNexis (RELX), LG Electronics, MongoDB, Motorola Solutions, Notion,
OCLC, Okta, Omnicom, Palo Alto Networks, Pinterest, Pure Storage, Q2, Red Hat, Reddit,
Relay (Relay Pro), Roblox, Salesforce, Snap, Snowflake, Sony Interactive Entertainment,
Twilio, Twitch, Veeva Systems, Verkada, Viavi Solutions, Waystar, WellSky, Wolters
Kluwer, Workday, Zoom, Zscaler and ZT Systems.

### Consumer internet and fintech

Affirm, Brex, Coinbase, DoorDash, iCapital Network, Lyft, Plaid, Remitly, Robinhood,
Rocket Companies and Stripe.

### Semiconductors and hardware

Analog Devices, Applied Materials, ASML, Cadence, GlobalFoundries, Intel, KLA, Marvell,
Micron, Moog, NXP, Samsung Semiconductor, Teledyne and Vishay.

### Autonomy, robotics and mobility

Applied Intuition, Aptiv, Bot Auto, Caterpillar, General Motors, Lucid Motors, Magna,
Symbotic, Toyota, Waymo and Zoox.

### Quant trading, asset management and market infrastructure

Akuna Capital, Ascensus, Broadridge, DRW, Fidelity Investments, Fidelity Investments
(campus), FIS, Five Rings, Hudson River Trading, IMC Trading, Invesco, Jane Street, Jump
Trading, LPL Financial Holdings, Nasdaq, Northern Trust, PIMCO, Point72, Raymond James
Financial, SS&C, State Street, Tower Research Capital, Vanguard and Virtu Financial.

### Banks, payments and insurance

AAA Club Alliance, Allstate, Bank of America, Capital One, Cigna Group, Citi, Equifax,
F.N.B. Corporation, Federal Reserve, Great American Insurance, Highmark Health,
Huntington Bank, Integrity Marketing Group, KeyBank, M&T Bank, Mastercard, PNC, RBC, TD
Bank, Texas Capital Bank, TransUnion, Travelers, Truist, Visa and Worldpay.

### Healthcare, life sciences, industrial and retail technology

3M, Abbott, Allegion, Becton Dickinson, Blissway, Caris, Carrier Global, Danaher
Corporation, DraftKings, Elevance Health, Freeform, GE Appliances, Genuine Parts
Company, Globus Medical, Hitachi, Johnson & Johnson, Lowe's, Medtronic, Merck, Novartis,
Philips, Scientific Games, SharkNinja, Technology Service Corporation, The Home Depot,
Uline and Viridien.

### Consulting, IT services and national labs

Accenture, AHEAD, Argonne National Laboratory, Brookhaven Lab, Guidehouse, Huron and ICF
International.

### Defense, aerospace and space

AeroVironment, Airbus, Allen Control Systems, Amentum, Anduril, Blue Origin, Boeing,
Booz Allen, CACI, CAE, CesiumAstro, Curtiss-Wright Corporation, Draper, GDIT, General
Dynamics Electric Boat, General Dynamics Mission Systems, Intuitive Research and
Technology, KBR, Leidos, Nightwing, Northrop Grumman, Northwood Space, Radiance
Technologies, Relativity Space, Rocket Lab, RTX, Saronic, Shield AI, Sierra Nevada
Corporation, SpaceX, Systems & Technology Research, The Aerospace Corporation, Torch
Technologies, True Anomaly, Vantor and Voyager Technologies.

### Large Workday boards

Workday reports at most 2,000 jobs for some tenants, and its listing order is not
always newest first, so large boards read with `options.max_jobs: 3000` and, where a
board would still exceed the limit or carry mostly unrelated roles, an
`applied_facets` filter: US jobs only, or technology and engineering job families
(Booz Allen, Leidos, Northrop Grumman, RTX, Amentum, Micron, PNC, Lowe's, Genuine
Parts). All Workday tenants share one request rate through the `myworkdayjobs.com`
and `myworkdaysite.com` entries in `settings.http.host_delays`, and Workday companies
are checked every four hours, two at a time, so the list stays well under Workday's
per-IP rate limit.

## How the less conventional sources are covered

| Source | Employers/examples | What is checked |
|---|---|---|
| MathWorks feed | MathWorks | Official RSS contains stable requisition IDs, location metadata and EDG titles; the main search page rejected automated requests during discovery. |
| HRM Direct / ClearCompany | Flexjet, DaySmart, Identity Digital, ChurnZero, LHP | Search must be submitted with `search=true`; table cells are parsed independently because some title links lack closing tags. Requisition/location pairs stay distinct. |
| iCIMS | SAS, Kinaxis | Follow visible pagination links and extract countries/locations from listing cards. A global hub can link to several subportals. |
| Branded Jibe API | Garmin | Read listing metadata from the site's public API and continue until its reported total is reached. |
| Avature | Bloomberg | Read result cards and follow the actual Next link; the requested page size can be ignored by the server. |
| Custom grouped HTML | COMSOL | Associate each job with its city/country heading, avoiding unnecessary detail-page requests. |
| Recruiterbox / Trakstar | Wolfram | Use the employer's public widget ID and listing API, with links back into the original careers widget. |
| Branded front end with an existing API | Esri, Geotab, D2L, Guidewire | Locate and verify the underlying board. A custom-looking front end is not necessarily a custom job database. |

### Coverage and matching limits

RSS and custom HTML sources that do not guarantee a complete snapshot are marked
incomplete for removal detection. Their observed jobs can still alert; a missing job
is not assumed closed. Paginated sources also remain incomplete when a cap or a
repeated page prevents a full listing. Parsing errors are reported as errors rather
than silently converted to zero openings.

Country matching uses listing text, country names and common city/state formats.
Unspecified remote jobs remain candidates for review. Unknown locations cannot pass
the location filter unless the scraper can retrieve the missing metadata. Workday
detail checks are bounded per pass, so some multi-location roles can take another
check to resolve. See the README's configuration section for the title rules and
eligibility limitations.

`notify_on_first_run` is disabled, so a newly added employer's first check records
its open roles as a baseline; only roles posted after that alert. Subsequent passes
use the existing deduplication and half-hour digest behavior.

## Expansion queue

These are research candidates, not enabled sources. Do not count them as monitored
until a live listing check succeeds. This queue is intentionally broader than the
first verified batch; it does not imply current hiring, degree eligibility or visa
sponsorship.

### Investigated sources needing additional work

| Employer | Remaining work |
|---|---|
| Epic | The public page embeds an open-job ID list and a larger position dictionary that includes unpublished/closed entries. A reader must intersect these and establish locations; category navigation links must not be treated as job postings. |
| SafetyCulture | Resolve the primary listing feed. An embedded board for Mitti also appears on the site and must not be mislabeled as SafetyCulture. |
| Postman | Resolve the current data source behind the open-positions page; older Greenhouse board names returned 404. |
| PhonePe | Resolve the current data source behind the jobs page; the guessed legacy board returned 404. |
| Freshworks | Its careers page points to SmartRecruiters; the API robot rules blocked our client. Assess an allowed public listing/feed before enabling it. |
| Zoho | Validate its Zoho Recruit listing/RSS, including pagination, locations and stable IDs. |
| PaperCut | Follow the current jobs board from its careers page and verify listing coverage. |
| Tyler Technologies | Its custom page has individual listings; validate pagination and location extraction before enabling it. |
| Octopus Deploy | Resolve the current board; the tested Ashby name was not valid. |
| Rokt | The inspected careers route entered a redirect loop; establish a working canonical source. |

### Additional employer families to investigate

#### Scientific, engineering and embedded software

Cadence, Synopsys/Ansys, Siemens Digital Industries Software, Altair, Bentley Systems,
Dassault Systèmes, Keysight, Emerson/NI, dSPACE, Vector Informatik, Hexagon, Certara,
Simulations Plus, Trimble, Zebra Technologies and Rockwell Automation.

#### Healthcare, research and enterprise systems

MEDITECH, InterSystems, athenahealth, IQVIA, Optum, GE HealthCare, Philips, Tempus,
Verily, Guardant Health, Medtronic, Siemens Healthineers and Veeva.

#### Financial infrastructure, insurers and regional software vendors

Jack Henry, FIS, Fiserv, Broadridge, Q2, Alkami, Duck Creek, CCC Intelligent Solutions,
FactSet, Morningstar, MSCI, S&P Global, Moody's, Verisk, Travelers, Progressive,
Liberty Mutual, Nationwide and regional banks' technology graduate programs.

#### Logistics, retail and operational technology

UPS, FedEx, J.B. Hunt, C.H. Robinson, Ryder, Expeditors, CSX, Norfolk Southern,
BNSF, Union Pacific, Target, Costco, Kroger, Publix, The Home Depot, Lowe's,
CarMax, AutoZone and Uline. Search their corporate technology organizations as
well as their main careers pages.

#### Canada

OpenText, Descartes Systems Group, Lightspeed, Vena, Dayforce, CGI, Enghouse,
Harris Computer and Jonas Software operating companies, Manulife, Sun Life,
Intact and Canadian banks' technology programs.

#### Australia

SEEK, REA Group, WiseTech Global, Iress, MYOB, TechnologyOne, Deputy, SiteMinder,
Envato, Atlassian, Canva, Telstra, Macquarie and Commonwealth Bank technology programs.

#### India

Groww, Zerodha, Hasura, InMobi, CleverTap, MoEngage, Uniphore, Icertis, Persistent,
LTIMindtree, KPIT, Zensar, Coforge and product engineering groups within larger
non-technology employers.

### Admission check for every new source

1. Establish the employer's actual current careers source, including regional portals.
2. Retrieve real listing data with stable IDs, titles, URLs and usable locations.
3. Validate pagination, empty results, moved pages and repeated-page handling.
4. Check unusual graduate-program titles against the filters.
5. Run the source validator without alerts, then add only verified sources to the
   shared configuration after the running code supports their readers.
