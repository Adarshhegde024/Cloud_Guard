# CloudGuard — Secure Digital Legacy Vault

CloudGuard is a complete, production-grade secure digital legacy management platform designed for college major project evaluation and real-world deployment.

An account owner securely stores encrypted documents and defines trusted recipients and nominees. The system continuously monitors owner vitality through live biometric check-ins. If the owner misses the configured check-in deadline and the configured grace period expires, the nominee workflow unlocks. The nominee submits identity evidence (death certificate) which is analyzed by an AI/OCR assistant against the owner's verified identity. A **human reviewer** makes the authoritative release decision. Only after reviewer approval and all security gates (such as Medical Hold) are satisfied can authorized recipients decrypt and view released documents directly in their browser using client-side Web Crypto AES-256-GCM.

---

## Core Features & Architecture

1. **Configurable Legacy Security Policy (Seconds, Minutes, Hours, Days)**
   - Policy values for **Check-in Frequency** and **Grace Period** can be configured in any supported time unit: `seconds`, `minutes`, `hours`, or `days`.
   - All values are strictly validated and normalized server-side to seconds (`checkin_frequency_seconds`, `grace_period_seconds`).
   - Enables instant live testing for evaluators and lecturers during college defense (e.g. 30 seconds check-in, 15 seconds grace period) while supporting production intervals (e.g. 30 days check-in, 72 hours grace period).

2. **Automated Background State Machine & Scheduler**
   - Implemented as an asynchronous task inside FastAPI's application lifespan.
   - Continuously evaluates deadlines and executes state transitions:
     `ACTIVE` ➔ `GRACE_PERIOD` ➔ `NOMINEE_CASE_ELIGIBLE` ➔ `LEGACY_REVIEW` ➔ `LEGACY_APPROVED` / `REJECTED` / `MORE_INFORMATION_REQUIRED`.
   - **Requirement 12 Enforcement:** If an owner completes a verified live biometric check-in while a death claim is pending, the claim is automatically invalidated and closed.

3. **Supabase Cloud Database Integration**
   - Connected with project instance: `https://yzzhvzuppqytbbqjpznu.supabase.co`
   - SQL schema DDL provided in `supabase_schema.sql` covering all 20 tables with Row Level Security (RLS) policies.
   - Fail-safe dual sync: the backend mirrors changes to Supabase in real time while maintaining SQLite as an offline-resilient local store.
   - Run `python backend/sync_to_supabase.py` to migrate local data to Supabase.

4. **AI & OCR Document Identity Extraction**
   - Multi-tier text extraction engine: Native PDF stream parsing (`pypdf`), Optical Character Recognition (`pytesseract` / OCR), and structured regular expression heuristic matching.
   - Compares extracted name, date of birth, and certificate number strictly against the **Registered Owner Identity** (never against the nominee).
   - Generates confidence scores, match statuses (`MATCH`, `MISMATCH`, `UNCHECKED`), and discrepancy logs.

5. **Human-in-the-Loop Reviewer Portal**
   - AI/OCR acts exclusively as an assistive decision aid; final authorization is strictly human-controlled.
   - Dedicated review dashboard with AI confidence badge, side-by-side identity verification cards, and discrepancy alerts.
   - Decisions: `APPROVE`, `REJECT`, or `REQUEST MORE INFORMATION`.

6. **In-Browser Web Crypto AES-256-GCM Client Decryption**
   - Recipient portal uses the browser's native hardware-accelerated Web Crypto API (`window.crypto.subtle.decrypt`).
   - Secure in-memory decryption ensures plaintext files never touch the recipient's local hard drive unencrypted.
   - Built-in previewers for PDF documents, images, audio, video, and notes.

7. **Cryptographic SHA-256 Hash-Chained Audit Trail**
   - Immutable audit ledger where every critical action links to the previous entry via SHA-256 hash chaining (`event_hash = SHA256(owner_id + event + created_at + previous_hash)`).
   - Accessible via the "Cryptographic Audit Trail" tab in the dashboard.

8. **Medical Hold Safety Gate**
   - Pauses all nominee workflows and freezes recipient document downloads during temporary medical emergencies.
   - Ending a hold requires a fresh, successful biometric check-in to re-engage monitoring.

---

## Quick Start (Windows PowerShell)

### 1. Install & Start Full Application (Frontend + Backend)
From the root project directory:
```powershell
npm install
npm run dev
```
- Frontend: `http://localhost:5173`
- Backend API: `http://localhost:8000`
- Interactive Swagger API Docs: `http://localhost:8000/docs`

### 2. Run End-to-End Test Suite
To verify all 12 core security gates, state transitions, AI OCR analysis, client decryption, and hash chaining:
```powershell
cd backend
.\.venv\Scripts\python.exe e2e_test.py
```

### 3. Supabase Cloud Database Setup
1. Open the [Supabase Dashboard](https://supabase.com/dashboard/project/yzzhvzuppqytbbqjpznu/sql).
2. Open the **SQL Editor** tab.
3. Copy the contents of `supabase_schema.sql` and click **Run**.
4. To sync existing local records to Supabase, run:
```powershell
cd backend
.\.venv\Scripts\python.exe sync_to_supabase.py
```

---

## College Defense Demo Script (How to Demonstrate to Lecturers)

1. **Owner Setup (30-Second Fast-Forward):**
   - Register a new owner account or log in.
   - Navigate to **Legacy security** in the sidebar.
   - Set **Check-in Frequency** to `30 Seconds` and **Grace Period** to `15 Seconds`. Click **Save security policy**.
   - Show lecturers the real-time normalized calculation (e.g. `= 30 seconds`).
2. **Document Vault:**
   - Upload an important document (PDF or Image). Point out AES-256-GCM encryption at rest.
   - Create a trusted recipient and assign the document.
   - Designate a nominee.
3. **Check-in Expiry & Nominee Eligibility:**
   - Wait 30 seconds. Point out the dashboard state updating to `GRACE_PERIOD`.
   - Wait 15 seconds. Point out state updating to `NOMINEE_CASE_ELIGIBLE`.
   - Sign in as the Nominee. Demonstrate that the case filing form is now unlocked.
4. **AI/OCR Evidence Analysis:**
   - Nominee uploads a death certificate.
   - Sign in to the Reviewer Portal (`reviewer@example.com` / `Reviewer123!`).
   - Show the **AI / OCR Document Assistant** card displaying side-by-side name and DOB comparison, confidence rating, and certificate numbers.
   - Reviewer clicks **Approve case**.
5. **In-Browser Recipient Decryption:**
   - Sign in as the Recipient.
   - Click **Decrypt & View in Browser**. Demonstrate the PDF or document rendering directly in the browser via Web Crypto API with zero external software needed.
6. **Cryptographic Audit Trail:**
   - Sign back in as Owner. Open **Check-in history** ➔ **Cryptographic Audit Trail**.
   - Show lecturers the unbroken SHA-256 hash chain mathematically proving event integrity.
