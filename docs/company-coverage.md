# Company coverage

The tracking list targets potential roles for a spring-2026 MS CS graduate in the
US, Canada, Australia and India whose resume centres on backend and full-stack work,
AI and LLM applications (RAG, agents), ML, data engineering, cloud platforms, and C++
systems and graphics. Employer selection covers AI companies, developer tools,
scientific software, healthcare, financial services, regional business software and
infrastructure. A company's recruiting platform does not determine whether it belongs
in the list. See [Resume alignment](#resume-alignment-september-2026) for how the
role keywords and employers were narrowed to that profile.

## Enabled employers

The 336 enabled employers are defined in [the configuration](../config/companies.yaml).
The latest source check is in [the validation report](company-validation.json).
Counts in that report are a dated observation, not a promise that a position remains
open or that its requirements fit the candidate. The report predates the US tech
expansion below; its config hash identifies the 67-employer list it checked.

### Scientific computing, simulation and healthcare

MathWorks, COMSOL, Wolfram, Kitware, Schrödinger, Esri, SAS, Flatiron Health, PathAI,
Natera, Recursion, Benchling and PointClickCare.

### Aviation, robotics, industrial technology and energy

Flexjet, Garmin, Zipline, Motional, Nuro, Torc Robotics, Waabi, Skydio, Samsara, Redwood
Materials, Gecko Robotics and Geotab.

### Financial technology and business software

Continental Finance, Bloomberg, Enova, Blend, Upstart, Alarm.com, Appian, Guidewire,
Razorpay, Harness, Wealthsimple, Zensurance, Jane, Xero, Culture Amp, Faire, DaySmart,
ChurnZero, Identity Digital, Kinaxis and D2L.

### Data infrastructure, cloud, security and developer tools

Cockroach Labs, SingleStore, Yugabyte, Grafana Labs, Imply, Fivetran, Auvik, Rubrik,
Druva, Elastic, ClickHouse, BrowserStack, Cohere, NVIDIA, Ashby, Ramp, Sentry, Discord,
Palantir, Airbyte, Confluent, Docker, GitLab, LaunchDarkly, Linear, Materialize, Neo4j,
PlanetScale, Redis, Render, Sourcegraph, Starburst, Supabase, Temporal and Vercel.

## US tech expansion (September 2026)

233 employers added on 2026-09-27 to widen US coverage. Candidates came from
employers posting US software, data, hardware, product and quant roles in the
SimplifyJobs new-grad dataset, on boards Role Radar can read. Each was kept only if
a live `role-radar list-matches` run with the default filters found at least five
matching US openings, and its matches were mostly software, data or IT roles rather
than civil, mechanical or HVAC engineering titles. Universities, state agencies,
staffing firms and small cleared-contract shops were left out. Defense, aerospace and
space employers were added too, then removed later that day (along with Technology
Service Corporation), since most of their roles need US citizenship or a security
clearance. The lists below show the current employers in each group, including
those added and minus those removed in the resume alignment pass.

### AI labs and AI-native startups

Abridge, Anthropic, Anyscale, Baseten, C3 AI, Cerebras, Character.AI, Chroma, Clay,
Cognition, CoreWeave, Cresta, Crusoe, Cursor, Decagon, ElevenLabs, Etched, Field AI,
Figure AI, Fireworks AI, Glean, Graphcore, Harvey, Labelbox, Lambda, LangChain,
LlamaIndex, Luma AI, Mercor, Modal, NewsBreak, OpenAI, OpenEvidence, Perplexity,
Pinecone, Replit, Scale AI, Sierra AI, Snorkel AI, Solace Health, Suno, Tenstorrent,
Together AI, Truveta, Unstructured, Weaviate, Writer and xAI.

### Compliance and GRC software

Drata, Hyperproof, OneTrust, RegScale, Secureframe, Thoropass and Vanta.

### Big tech, cloud, enterprise software and media

Adobe, Airbnb, Alkami Technology, Asana, Asure, Autodesk, Axon, Blue Yonder, Broadcom,
Ciena, Cisco, Clearwater Analytics, CoStar Group, Cox Enterprises, CrowdStrike,
Databricks, Datadog, Disney, Dropbox, eBay, Epic Games, Expedia, F5, Figma, Hewlett
Packard Enterprise, HP, iHeartMedia, LexisNexis (RELX), LG Electronics, MongoDB,
Motorola Solutions, Notion, OCLC, Okta, Omnicom, Palo Alto Networks, Pinterest, Pure
Storage, Q2, Red Hat, Reddit, Relay (Relay Pro), Riot Games, Roblox, Salesforce, Snap,
Snowflake, Sony Interactive Entertainment, Spotify, Squarespace, Twilio, Twitch, Veeva
Systems, Verkada, Viavi Solutions, Waystar, WellSky, Wolters Kluwer, Workday, Zoom and
Zscaler.

### Consumer internet and fintech

Affirm, Block, Brex, Checkr, Chime, Coinbase, DoorDash, Duolingo, Gusto, iCapital
Network, Instacart, Klaviyo, Lyft, Mercury, Nextdoor, Plaid, Remitly, Robinhood, Rocket
Companies, Stripe and Toast.

### Semiconductors and hardware

AMD, Analog Devices, Applied Materials, ASML, Cadence, Intel, KLA, Micron and Samsung
Semiconductor.

### Autonomy, robotics and mobility

Applied Intuition, Aptiv, Bot Auto, Caterpillar, General Motors, Lucid Motors, Magna,
Symbotic, Toyota, Waymo and Zoox.

### Quant trading, asset management and market infrastructure

Akuna Capital, Broadridge, DRW, Fidelity Investments, Fidelity Investments (campus),
FIS, Five Rings, Hudson River Trading, IMC Trading, Invesco, Jane Street, Jump Trading,
LPL Financial Holdings, Nasdaq, Northern Trust, Point72, SS&C, State Street, T. Rowe
Price, Tower Research Capital, Vanguard and Virtu Financial.

### Banks, payments and insurance

Allstate, Bank of America, Capital One, Cigna Group, Citi, Equifax, F.N.B. Corporation,
Freddie Mac, GEICO, Huntington Bank, M&T Bank, Mastercard, PNC, RBC, TD Bank,
TransUnion, Travelers, Truist, Visa and Worldpay.

### Healthcare, life sciences, industrial and retail technology

3M, Abbott, Allegion, Blissway, Caris, Danaher Corporation, DraftKings, Elevance Health,
Freeform, Genuine Parts Company, Globus Medical, Hitachi, Johnson & Johnson, Lowe's,
Medtronic, Merck, Novartis, Philips, Scientific Games, SharkNinja, The Home Depot, Uline
and Viridien.

### Consulting, IT services and national labs

Accenture, AHEAD, Argonne National Laboratory, Brookhaven Lab, Guidehouse, Huron and ICF
International.

### Large Workday boards

Workday reports at most 2,000 jobs for some tenants, and its listing order is not
always newest first, so large boards read with `options.max_jobs: 3000` and, where a
board would still exceed the limit or carry mostly unrelated roles, an
`applied_facets` filter: US jobs only, or technology and engineering job families
(Micron, PNC, Lowe's, Genuine Parts). All Workday tenants share one request rate
through the `myworkdayjobs.com` and `myworkdaysite.com` entries in `settings.http.host_delays`, and Workday companies
are checked every four hours, two at a time, so the list stays well under Workday's
per-IP rate limit. Between those, a quick check every 10 minutes reads each board's newest
page. In a September 2026 sample, 9 of 10 boards listed newest first (NVIDIA, Bank of
America, Capital One, Cisco, Boeing, Salesforce, Adobe, Visa, and Northrop Grumman nearly);
Intel did not, so its new jobs are found by the full check.

## Big tech and more career platforms (September 2026)

38 employers added on 2026-09-27, on career sites Role Radar couldn't read before:

- Their own career sites: Amazon, Apple, Google, Meta, Microsoft, Netflix and TikTok.
- Eightfold: Qualcomm, PayPal, John Deere, Eaton and Boston Scientific.
- Oracle Cloud HCM: JPMorgan Chase, Oracle, Texas Instruments, Honeywell, American
  Express, Goldman Sachs (lateral hiring site), Ford, GM Financial, BNY, Citizens,
  DTCC, Verisk, EXL, Fortinet, onsemi, Coherent, Nokia, Emerson, Cummins, Vertiv,
  Fortive, Denso, Hologic, UL Solutions, ADT and Albertsons.

Eaton, Texas Instruments, Citizens, onsemi, Coherent, Cummins, Vertiv, Denso, Hologic,
UL Solutions, ADT and Albertsons were removed again in the resume alignment pass.

Each source was checked against its robots.txt (RFC 9309 matching) and read live.
Limits worth knowing:

- Google's robots.txt allows a search's first page only, so each check sees the 20
  newest early-career and 20 newest mid-level US jobs. That covers what Google posts
  between checks, but never the full list.
- Amazon, Apple, Microsoft and the Oracle sites are read newest first up to a cap
  (see each company's options), so they're never removal snapshots.
- Meta's search page is built in the browser; the scraper uses its job sitemap and
  reads each new job's page once (up to 2 minutes of pages a check, 4 a second), so
  the first ~3 checks work through its ~1,000 open jobs; after that a new posting is
  read at the next check. `max_alert_age_days: 14` keeps those older jobs from alerting.
- TikTok's API filters by city only, so each check reads all ~4,300 jobs (~18 MB)
  and keeps US ones.

Not added, and why: SmartRecruiters (ServiceNow, Western Digital, AbbVie, Visa...)
disallows its posting API to all crawlers but LinkedIn's; its public pages group
jobs by city and would need their own reader. ByteDance's own site (jobs.bytedance.com)
hasn't been looked at yet. Netflix's Workday board (and Walmart's and Comcast's)
answers HTTP 422; AMD's does too, but its Jibe career site is read instead.
American Express's Eightfold API answers 404; its Oracle site is used.

## Resume alignment (September 2026)

On 2026-09-27 the default role keywords and the employer list were narrowed to the
resume: backend and full-stack engineering (React, Django, FastAPI, Kafka, Redis),
AI and LLM applications (RAG, embeddings, agents), ML and computer vision, data
engineering, AWS serverless and platform work, test automation, and C++ systems,
performance and graphics work (a ray tracer, rasterizer and OpenGL renderer).

Every job title already stored in the state table (about 100,000 open jobs) was
matched offline against the old and new filters, so the effect was measured without
scraping. At the employers that stayed, open title matches fell from about 9,600 to
about 6,200 before the experience filter.

**Role keywords dropped**, since the resume doesn't show that work: generic product
management, product owner and analyst roles (Associate Product Manager and Technical
Program Manager stay); business, data, BI, systems, IT and quantitative analysts;
security, SOC, penetration testing and detection roles; network and system
administration; support, cloud support, implementation and technical consulting;
mobile (iOS/Android) specialists; hardware-side systems, embedded, firmware,
robotics, simulation, test and automation engineers; field "applications" and
customer engineers; and research scientists, which mostly ask for a PhD.

**Role keywords added**: UI and Python engineers; computer graphics and gameplay
engineers; LLMOps, GenAI, agent, conversational AI, deep learning, perception,
inference, search and information retrieval roles; AI residencies; GRC engineers;
QA automation and software test roles; and forward-deployed and AI deployment
engineers.

**Titles now excluded** everywhere: ERP and CRM platform developers (ServiceNow,
SAP, Salesforce, Pega, MuleSoft and similar), technical-writing "developers"
(courseware, content, documentation), hardware and manufacturing titles (firmware,
BIOS, PCB, FPGA, ASIC, RTL, CNC, CMM, PLC, HVAC, design release, process integration,
supplier, technician), and product managers other than associate product managers.
Amazon's search no longer spends its newest-500 budget on hardware, database
administration, BI or security categories.

**35 employers removed**, each with at most about five matching titles under the new
filters, most of them hardware, manufacturing or non-software roles:

- Hardware and semiconductors: Vishay, Texas Instruments, Marvell, Coherent,
  GlobalFoundries, onsemi, NXP and ZT Systems; Moog and Teledyne, which are mostly
  defense and aerospace like the employers removed earlier.
- Industrial and devices: Form Energy, LHP Engineering Solutions, Eaton, Denso, Vertiv,
  Cummins, Carrier Global, GE Appliances, Hologic, Becton Dickinson, UL Solutions and
  ADT.
- Finance, insurance and other: KeyBank, Citizens, Texas Capital Bank, Great American
  Insurance, Integrity Marketing Group, AAA Club Alliance, Highmark Health, Raymond
  James Financial, PIMCO, Ascensus, Federal Reserve, Gartner and Albertsons (whose
  matches were in-store "Front End" roles).

**70 employers added**, each board read once to confirm it answers:

- AI and LLM companies, including LLM infrastructure and vector databases: Abridge,
  Anyscale, Baseten, C3 AI, Character.AI, Chroma, Clay, Cognition, CoreWeave, Cresta,
  Decagon, ElevenLabs, Figure AI, Fireworks AI, Labelbox, LangChain, LlamaIndex, Luma
  AI, Mercor, Modal, OpenEvidence, Pinecone, Snorkel AI, Suno, Unstructured, Weaviate
  and Writer.
- Compliance and GRC software, close to the NIST 800-53 audit work: Drata, Hyperproof,
  OneTrust, RegScale, Secureframe, Thoropass and Vanta.
- Developer tools and data infrastructure (Next.js, Postgres, Kafka, Redis, Docker):
  Airbyte, Confluent, Docker, GitLab, LaunchDarkly, Linear, Materialize, Neo4j,
  PlanetScale, Redis, Render, Sourcegraph, Starburst, Supabase, Temporal and Vercel.
- Consumer, fintech and SaaS: Airbnb, Block, Checkr, Chime, Dropbox, Duolingo, Gusto,
  Instacart, Klaviyo, Mercury, Nextdoor, Spotify, Squarespace and Toast.
- Games and graphics: Epic Games, Riot Games and AMD.
- Baltimore and Washington area: T. Rowe Price, Freddie Mac and GEICO.

Some had no matching opening on the day they were added (Airbnb, Docker and Linear
were posting only senior roles); they stay because they hire for the profile.

Not added: Cloudflare's Greenhouse board gives "Hybrid" or "Distributed" as each job's
location instead of a city, so the location filter can't tell US jobs apart without a
scraper change. No readable board was found for dbt Labs, Weights & Biases, Rippling,
Retool, Hugging Face, Mistral AI, Unity, Atlassian, Grammarly, Hebbia or Fannie Mae.

## How the less conventional sources are covered

| Source | Employers/examples | What is checked |
|---|---|---|
| MathWorks feed | MathWorks | Official RSS contains stable requisition IDs, location metadata and EDG titles; the main search page rejected automated requests during discovery. |
| HRM Direct / ClearCompany | Flexjet, DaySmart, Identity Digital, ChurnZero | Search must be submitted with `search=true`; table cells are parsed independently because some title links lack closing tags. Requisition/location pairs stay distinct. |
| iCIMS | SAS, Kinaxis | Follow visible pagination links and extract countries/locations from listing cards. A global hub can link to several subportals. |
| Branded Jibe API | Garmin, AMD | Read listing metadata from the site's public API and continue until its reported total is reached. |
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
