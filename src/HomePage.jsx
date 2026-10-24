import React, { useState } from 'react'

export default function HomePage({ onGetStarted, onSignIn, Icon }) {
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false)
  const [activeRoleTab, setActiveRoleTab] = useState('owner')
  const [openFaq, setOpenFaq] = useState(0)

  const roles = {
    owner: {
      title: 'Vault Owner (You)',
      badge: 'Zero-Knowledge Control',
      desc: 'You hold complete sovereignty over your digital legacy. Store critical documents encrypted with AES-256-GCM, assign assets to beneficiaries, set check-in intervals, and toggle emergency Medical Holds whenever needed.',
      features: [
        'Client-side preview with AES-256-GCM zero-plaintext storage',
        'Periodic multimodal AI live check-in (Face + Liveness + Voice)',
        'One-click emergency Medical Hold if hospitalized or incapacitated',
        'Explicit document-by-document release authorization gate'
      ],
      actionText: 'Create Your Vault',
      actionRole: 'register',
      isPrimary: true
    },
    nominee: {
      title: 'Trusted Nominee',
      badge: 'Independent Filing Portal',
      desc: 'Designate a trusted confidant or legal representative. In the event of death, your nominee accesses their own isolated portal to submit death certificate evidence and track reviewer decisions.',
      features: [
        'Dedicated role-scoped credentials (never sees owner vaults)',
        'Encrypted death certificate upload (PDF, JPG, PNG up to 10MB)',
        'Real-time timeline tracking (Review, Inquiry, Decision)',
        'Direct case messaging with authorized review officers'
      ],
      actionText: 'Nominee Portal Sign-In',
      actionRole: 'nominee-login',
      isPrimary: false
    },
    recipient: {
      title: 'Beneficiaries & Recipients',
      badge: 'Protected Asset Delivery',
      desc: 'Family members, business partners, or heirs receive role-scoped access only to the exact documents you assigned to them—and strictly after all security gates and reviewer verifications clear.',
      features: [
        'Single-use claim token activation with secure password setup',
        'Strict recipient isolation—no visibility into other beneficiaries',
        'Decrypted high-security download only after approved clearance',
        'Audited access logs with timestamped confirmation'
      ],
      actionText: 'Recipient Portal Sign-In',
      actionRole: 'recipient-login',
      isPrimary: false
    },
    reviewer: {
      title: 'Legal & Review Authority',
      badge: 'Independent Verification Gate',
      desc: 'Independent reviewers inspect submitted death evidence, verify civil registry legitimacy, and adjudicate release eligibility before any asset can be considered for release.',
      features: [
        'Isolated review queue with tamper-evident evidence inspection',
        'Three-state decision flow: Approved, Rejected, More Info Needed',
        'Mandatory reason logging and reviewer audit logging',
        'No direct access to underlying owner vault documents'
      ],
      actionText: 'Reviewer Portal Sign-In',
      actionRole: 'reviewer-login',
      isPrimary: false
    }
  }

  const faqs = [
    {
      q: 'What happens if I miss a scheduled check-in?',
      a: 'A missed check-in never automatically releases your files. Unlike naive dead-man switches, CloudGuard requires multi-party confirmation: your designated nominee must submit official death evidence, and an independent reviewer must adjudicate and approve the case before any release can be considered.'
    },
    {
      q: 'Can anyone or CloudGuard staff read my documents while I am alive?',
      a: 'No. All documents are encrypted at rest with military-grade AES-256-GCM using unique random nonces. Ciphertext is stored securely, and files can only be accessed with your private authenticated credentials. The system is designed with zero-knowledge principles.'
    },
    {
      q: 'What is the Emergency Medical Hold?',
      a: 'If you are hospitalized, undergoing surgery, or temporarily incapacitated, you can activate the Medical Hold from your dashboard with one tap. While active or awaiting verification, all document releases and case executions are completely frozen. The freeze can only be lifted by you completing a fresh, successful multimodal biometric check-in.'
    },
    {
      q: 'How does the multimodal AI live check-in prevent spoofing?',
      a: 'CloudGuard uses a three-layer neural pipeline: (1) DeepFace compares live video frames against your 5 enrolled face vectors, (2) DeepFace neural anti-spoofing detects presentation attacks such as photos, masks, or screens, and (3) faster-whisper speech recognition validates a randomly prompted challenge phrase spoken in real time.'
    },
    {
      q: 'How do nominees and recipients receive access without email?',
      a: 'CloudGuard generates cryptographic, single-use claim tokens. Owners can copy and share these secure claim links directly via WhatsApp, SMS, or in-person legal documents. Email providers like Resend are completely optional.'
    }
  ]

  const currentRole = roles[activeRoleTab]

  return (
    <div className="home-container">
      {/* Top Announcement Bar */}
      <div className="home-announcement">
        <div className="home-announcement-content">
          <span className="live-dot" />
          <span><strong>CloudGuard 2.0 Live:</strong> Multimodal AI Biometrics, AES-256-GCM Zero-Knowledge Vault &amp; Multi-Party Release Protocol</span>
        </div>
      </div>

      {/* Navigation Bar */}
      <header className="home-header">
        <div className="home-header-inner">
          <a href="#" className="brand home-brand">
            <span className="brand-mark"><Icon name="shield" size={20} /></span>
            <span>cloudguard<span className="brand-period">.</span></span>
          </a>

          <nav className="home-nav" aria-label="Main Navigation">
            <a href="#features">Features</a>
            <a href="#how-it-works">How It Works</a>
            <a href="#checkin-ai">AI Biometrics</a>
            <a href="#portals">Role Portals</a>
            <a href="#faq">FAQ</a>
          </nav>

          <div className="home-header-actions">
            <button
              type="button"
              className="button button-quiet button-small"
              onClick={() => onSignIn('login')}
            >
              Sign In
            </button>
            <button
              type="button"
              className="button button-primary button-small"
              onClick={onGetStarted}
            >
              Create Vault <Icon name="arrow" size={14} />
            </button>
            <button
              type="button"
              className="icon-button mobile-menu-btn"
              onClick={() => setMobileMenuOpen(!mobileMenuOpen)}
              aria-label="Toggle navigation menu"
            >
              <Icon name={mobileMenuOpen ? 'close' : 'menu'} size={20} />
            </button>
          </div>
        </div>

        {/* Mobile Navigation Drawer */}
        {mobileMenuOpen && (
          <div className="mobile-drawer">
            <nav className="mobile-nav-links">
              <a href="#features" onClick={() => setMobileMenuOpen(false)}>Features</a>
              <a href="#how-it-works" onClick={() => setMobileMenuOpen(false)}>How It Works</a>
              <a href="#checkin-ai" onClick={() => setMobileMenuOpen(false)}>AI Biometrics</a>
              <a href="#portals" onClick={() => setMobileMenuOpen(false)}>Role Portals</a>
              <a href="#faq" onClick={() => setMobileMenuOpen(false)}>FAQ</a>
            </nav>
            <div className="mobile-drawer-actions">
              <button
                type="button"
                className="button button-secondary button-full"
                onClick={() => { setMobileMenuOpen(false); onSignIn('login') }}
              >
                Sign In
              </button>
              <button
                type="button"
                className="button button-primary button-full"
                onClick={() => { setMobileMenuOpen(false); onGetStarted() }}
              >
                Create Your Free Vault <Icon name="arrow" size={16} />
              </button>
            </div>
          </div>
        )}
      </header>

      <main>
        {/* Hero Section */}
        <section className="home-hero" id="hero">
          <div className="hero-glow-1" />
          <div className="hero-glow-2" />
          <div className="home-hero-inner">
            <div className="hero-copy">
              <div className="hero-badge">
                <span className="hero-badge-dot" />
                <span>Next-Gen Post-Mortem Digital Vault</span>
              </div>
              <h1 className="hero-title">
                Keep what matters most.<br />
                <span className="hero-gradient-text">Protected today.</span><br />
                Delivered tomorrow.
              </h1>
              <p className="hero-subtitle">
                CloudGuard fuses military-grade AES-256-GCM encryption with local AI biometric check-in (FaceNet, liveness anti-spoofing, and speech challenge) and strict multi-party legal release gates. Your documents stay private, safe, and delivered only when the time comes.
              </p>
              <div className="hero-cta-group">
                <button
                  type="button"
                  className="button button-primary hero-btn-main"
                  onClick={onGetStarted}
                >
                  <Icon name="lock" size={18} /> Open Your Vault
                </button>
                <button
                  type="button"
                  className="button button-secondary hero-btn-sub"
                  onClick={() => onSignIn('login')}
                >
                  Owner Sign In
                </button>
                <a href="#portals" className="hero-link-roles">
                  <span>Nominee &amp; Recipient Portals</span> <Icon name="arrow" size={14} />
                </a>
              </div>

              <div className="hero-trust-row">
                <div className="trust-item">
                  <Icon name="shield" size={16} /> <span>AES-256-GCM Encrypted</span>
                </div>
                <div className="trust-item">
                  <Icon name="scan" size={16} /> <span>3-Factor Biometric Liveness</span>
                </div>
                <div className="trust-item">
                  <Icon name="heart" size={16} /> <span>Medical Hold Safe</span>
                </div>
                <div className="trust-item">
                  <Icon name="key" size={16} /> <span>Zero Plaintext Storage</span>
                </div>
              </div>
            </div>

            {/* Interactive Hero Visual Showcase */}
            <div className="hero-visual">
              <div className="hero-card-glass">
                <div className="hero-card-header">
                  <div className="hero-card-status">
                    <span className="live-dot" />
                    <span>VAULT SYSTEM: ARMED &amp; SECURE</span>
                  </div>
                  <span className="hero-card-tag">AES-256-GCM</span>
                </div>

                <div className="hero-interactive-dashboard">
                  <div className="hero-metric-tile">
                    <div className="hero-metric-icon purple-tile">
                      <Icon name="scan" size={20} />
                    </div>
                    <div className="hero-metric-content">
                      <small>BIOMETRIC CHECK-IN</small>
                      <b>Every 30 Days</b>
                      <span className="metric-tag-success">✓ FaceNet &bull; Liveness &bull; Voice</span>
                    </div>
                  </div>

                  <div className="hero-metric-tile">
                    <div className="hero-metric-icon emerald-tile">
                      <Icon name="heart" size={20} />
                    </div>
                    <div className="hero-metric-content">
                      <small>EMERGENCY MEDICAL HOLD</small>
                      <b>Standing Guard</b>
                      <span className="metric-tag-muted">One-Click Freeze Protocol</span>
                    </div>
                  </div>

                  <div className="hero-metric-tile">
                    <div className="hero-metric-icon blue-tile">
                      <Icon name="file" size={20} />
                    </div>
                    <div className="hero-metric-content">
                      <small>ENCRYPTED VAULT ITEMS</small>
                      <b>Wills, Deeds, Passwords</b>
                      <span className="metric-tag-muted">AES-GCM Unique 96-Bit Nonce</span>
                    </div>
                  </div>

                  <div className="hero-metric-tile">
                    <div className="hero-metric-icon orange-tile">
                      <Icon name="users" size={20} />
                    </div>
                    <div className="hero-metric-content">
                      <small>SEPARATED ROLE ACCESS</small>
                      <b>Nominee &bull; Recipient &bull; Reviewer</b>
                      <span className="metric-tag-muted">Strict Zero-Crossover Isolation</span>
                    </div>
                  </div>
                </div>

                <div className="hero-card-footer">
                  <span className="crypto-hash">SHA-256 Chain: c77372d2...9ac70df0a8</span>
                  <span className="crypto-badge"><Icon name="lock" size={12} /> Encrypted at rest</span>
                </div>
              </div>
            </div>
          </div>
        </section>

        {/* Stats Strip */}
        <section className="stats-strip">
          <div className="stats-container">
            <div className="stat-item">
              <span className="stat-number">256-bit</span>
              <span className="stat-desc">AES-GCM Authenticated Encryption</span>
            </div>
            <div className="stat-item">
              <span className="stat-number">3-Layer</span>
              <span className="stat-desc">Multimodal AI Check-In Verification</span>
            </div>
            <div className="stat-item">
              <span className="stat-number">4 Portals</span>
              <span className="stat-desc">Strictly Separated Role Boundaries</span>
            </div>
            <div className="stat-item">
              <span className="stat-number">0-Leak</span>
              <span className="stat-desc">Zero Plaintext Exposure Guarantee</span>
            </div>
          </div>
        </section>

        {/* Feature Pillars Grid */}
        <section className="home-section" id="features">
          <div className="section-head">
            <span className="eyebrow">ENGINEERED FOR SENSITIVE ASSETS</span>
            <h2>Security You Can Trust With Your Entire Legacy</h2>
            <p>Built from first principles to ensure your wishes are honored without trusting third-party custodians or relying on fragile dead-man timers.</p>
          </div>

          <div className="features-grid">
            <div className="feature-card">
              <div className="feature-icon-box"><Icon name="lock" size={22} /></div>
              <h3>AES-256-GCM Vault</h3>
              <p>User documents are encrypted at rest with unique 12-byte initialization nonces. Downloads export as cryptographically protected <code>.cgenc</code> ciphertext.</p>
            </div>

            <div className="feature-card">
              <div className="feature-icon-box"><Icon name="scan" size={22} /></div>
              <h3>Multimodal AI Check-In</h3>
              <p>FaceNet facial vector matching, DeepFace neural anti-spoofing against masks and screens, and faster-whisper challenge phrase audio verification.</p>
            </div>

            <div className="feature-card">
              <div className="feature-icon-box"><Icon name="heart" size={22} /></div>
              <h3>Emergency Medical Hold</h3>
              <p>Incapacitated or in the hospital? One click freezes all releases. Unfreezing strictly requires you to perform a fresh, authenticated live biometric check-in.</p>
            </div>

            <div className="feature-card">
              <div className="feature-icon-box"><Icon name="users" size={22} /></div>
              <h3>Separated Role Architecture</h3>
              <p>Dedicated standalone portals for Nominees, Beneficiaries, and Reviewers. Nominees never access owner files; beneficiaries see only their designated files.</p>
            </div>

            <div className="feature-card">
              <div className="feature-icon-box"><Icon name="clock" size={22} /></div>
              <h3>Forensic Audit Chaining</h3>
              <p>Every login, document upload, share modification, check-in, and review decision is cryptographically chained with SHA-256 hashes for non-repudiation.</p>
            </div>

            <div className="feature-card">
              <div className="feature-icon-box"><Icon name="shield" size={22} /></div>
              <h3>Private Local Inference</h3>
              <p>Check-in video clips are analyzed in temporary memory and permanently deleted immediately after inference. No biometric recordings are ever retained.</p>
            </div>
          </div>
        </section>

        {/* How It Works */}
        <section className="home-section section-alternate" id="how-it-works">
          <div className="section-head">
            <span className="eyebrow">THE LIFECYCLE OF PROTECTION</span>
            <h2>How CloudGuard Secures Your Digital Legacy</h2>
            <p>A proven, fail-closed 3-step lifecycle protecting your family from loss, probate delays, and digital asset lockouts.</p>
          </div>

          <div className="steps-container">
            <div className="step-card">
              <div className="step-number">01</div>
              <div className="step-icon"><Icon name="file" size={24} /></div>
              <h3>Store &amp; Encrypt</h3>
              <p>Upload critical documents, financial records, crypto keys, wills, and personal messages. Each file is encrypted with AES-256-GCM and assigned to specific beneficiaries.</p>
              <div className="step-tag">Initial Enrollment</div>
            </div>

            <div className="step-card">
              <div className="step-number">02</div>
              <div className="step-icon"><Icon name="scan" size={24} /></div>
              <h3>Live Periodic Check-Ins</h3>
              <p>Complete a quick 10-second webcam check-in once a month. CloudGuard verifies your face, liveness, and voice. If health issues arise, trigger a Medical Hold to pause releases.</p>
              <div className="step-tag">Active Protection</div>
            </div>

            <div className="step-card">
              <div className="step-number">03</div>
              <div className="step-icon"><Icon name="key" size={24} /></div>
              <h3>Multi-Gate Verified Release</h3>
              <p>When the time comes, your nominee uploads certified proof of death. An independent reviewer validates authenticity, and only owner-approved assets are released.</p>
              <div className="step-tag">Post-Mortem Execution</div>
            </div>
          </div>
        </section>

        {/* AI Check-In Deep Dive */}
        <section className="home-section" id="checkin-ai">
          <div className="checkin-ai-box">
            <div className="checkin-ai-copy">
              <span className="eyebrow">MULTIMODAL ON-DEVICE BIOMETRICS</span>
              <h2>3-Factor Verification That Fails Closed</h2>
              <p>
                Unlike basic passcodes that can be stolen or single-factor timers, CloudGuard requires three independent factors to confirm your well-being in a single 10-second recording:
              </p>

              <div className="ai-factors-list">
                <div className="ai-factor-item">
                  <div className="ai-factor-badge">1</div>
                  <div>
                    <b>FaceNet Facial Vector Comparison</b>
                    <p>Compares live frames against your 5 baseline enrollment photos using cosine distance (distance $\le$ 0.24 required across majority of frames).</p>
                  </div>
                </div>

                <div className="ai-factor-item">
                  <div className="ai-factor-badge">2</div>
                  <div>
                    <b>Neural Anti-Spoofing &amp; Liveness</b>
                    <p>Detects spoofing attempts including static photographs, video screen replays, paper cutouts, and 3D silicone presentation attacks.</p>
                  </div>
                </div>

                <div className="ai-factor-item">
                  <div className="ai-factor-badge">3</div>
                  <div>
                    <b>Speech-to-Text Phrase Verification</b>
                    <p>Faster-whisper transcribes audio in real time and fuzzy-matches against a randomized challenge sentence to guarantee fresh presence.</p>
                  </div>
                </div>
              </div>

              <div className="ai-privacy-note">
                <Icon name="lock" size={16} />
                <span><strong>Privacy Guarantee:</strong> Videos are processed in memory and destroyed immediately. Only the timestamp and outcome status are saved.</span>
              </div>
            </div>

            <div className="checkin-ai-visual">
              <div className="checkin-mockup-frame">
                <div className="checkin-mockup-header">
                  <span className="live-dot" />
                  <span>RECORDING LIVE (10.0s)</span>
                  <span className="timer-badge">00:08</span>
                </div>
                <div className="checkin-mockup-body">
                  <div className="mockup-face-frame">
                    <div className="mockup-target-corners" />
                    <Icon name="scan" size={60} />
                    <span className="mockup-target-label">FaceNet Vector Active: 0.18</span>
                  </div>
                  <div className="mockup-phrase-box">
                    <small>SAY THIS PHRASE ALOUD:</small>
                    <p>&ldquo;Today I am confirming my CloudGuard check-in.&rdquo;</p>
                  </div>
                  <div className="mockup-progress">
                    <div className="mockup-progress-fill" style={{ width: '80%' }} />
                  </div>
                </div>
              </div>
            </div>
          </div>
        </section>

        {/* Role Portals Interactive Switcher */}
        <section className="home-section section-alternate" id="portals">
          <div className="section-head">
            <span className="eyebrow">ROLE-BASED SEGREGATION</span>
            <h2>Dedicated Portals For Every Stakeholder</h2>
            <p>CloudGuard maintains strict boundary separation. Click any role below to explore its specific capabilities and sign-in portal.</p>
          </div>

          <div className="portals-container">
            <div className="role-tabs">
              <button
                type="button"
                className={`role-tab-btn ${activeRoleTab === 'owner' ? 'active' : ''}`}
                onClick={() => setActiveRoleTab('owner')}
              >
                <Icon name="lock" size={18} /> Vault Owner
              </button>
              <button
                type="button"
                className={`role-tab-btn ${activeRoleTab === 'nominee' ? 'active' : ''}`}
                onClick={() => setActiveRoleTab('nominee')}
              >
                <Icon name="shield" size={18} /> Trusted Nominee
              </button>
              <button
                type="button"
                className={`role-tab-btn ${activeRoleTab === 'recipient' ? 'active' : ''}`}
                onClick={() => setActiveRoleTab('recipient')}
              >
                <Icon name="users" size={18} /> Beneficiary / Recipient
              </button>
              <button
                type="button"
                className={`role-tab-btn ${activeRoleTab === 'reviewer' ? 'active' : ''}`}
                onClick={() => setActiveRoleTab('reviewer')}
              >
                <Icon name="scan" size={18} /> Legal Reviewer
              </button>
            </div>

            <div className="role-detail-card">
              <div className="role-detail-header">
                <div>
                  <span className="soft-tag">{currentRole.badge}</span>
                  <h3>{currentRole.title}</h3>
                </div>
                <button
                  type="button"
                  className={`button ${currentRole.isPrimary ? 'button-primary' : 'button-secondary'}`}
                  onClick={() => currentRole.actionRole === 'register' ? onGetStarted() : onSignIn(currentRole.actionRole)}
                >
                  {currentRole.actionText} <Icon name="arrow" size={16} />
                </button>
              </div>

              <p className="role-detail-desc">{currentRole.desc}</p>

              <div className="role-features-grid">
                {currentRole.features.map((feat, idx) => (
                  <div key={idx} className="role-feat-item">
                    <span className="role-check-icon"><Icon name="check" size={15} /></span>
                    <span>{feat}</span>
                  </div>
                ))}
              </div>
            </div>
          </div>
        </section>

        {/* FAQ Accordion */}
        <section className="home-section" id="faq">
          <div className="section-head">
            <span className="eyebrow">COMMON QUESTIONS</span>
            <h2>Frequently Asked Questions</h2>
            <p>Everything you need to know about CloudGuard&rsquo;s security model, release protocol, and privacy guarantees.</p>
          </div>

          <div className="faq-accordion">
            {faqs.map((faq, index) => {
              const isOpen = openFaq === index
              return (
                <div key={index} className={`faq-card ${isOpen ? 'faq-open' : ''}`}>
                  <button
                    type="button"
                    className="faq-question-btn"
                    onClick={() => setOpenFaq(isOpen ? null : index)}
                    aria-expanded={isOpen}
                  >
                    <span>{faq.q}</span>
                    <span className="faq-chevron">
                      <Icon name="chevron" size={18} />
                    </span>
                  </button>
                  {isOpen && (
                    <div className="faq-answer">
                      <p>{faq.a}</p>
                    </div>
                  )}
                </div>
              )
            })}
          </div>
        </section>

        {/* Bottom Call to Action Banner */}
        <section className="home-cta-banner">
          <div className="cta-banner-inner">
            <h2>Your Life&rsquo;s Most Important Documents Deserve Uncompromising Protection.</h2>
            <p>Set up your private encrypted vault in under 3 minutes. Zero third-party trackers, zero plaintext exposure, and total peace of mind for you and your family.</p>
            <div className="cta-banner-buttons">
              <button
                type="button"
                className="button button-light hero-btn-main"
                onClick={onGetStarted}
              >
                <Icon name="lock" size={18} /> Create Your Free Vault
              </button>
              <button
                type="button"
                className="button button-quiet"
                onClick={() => onSignIn('login')}
              >
                Sign In to Existing Vault
              </button>
            </div>
          </div>
        </section>
      </main>

      {/* Footer */}
      <footer className="home-footer">
        <div className="home-footer-inner">
          <div className="footer-col-main">
            <a href="#" className="brand footer-brand">
              <span className="brand-mark"><Icon name="shield" size={18} /></span>
              <span>cloudguard<span className="brand-period">.</span></span>
            </a>
            <p>The sovereign digital-legacy vault engineered for life&rsquo;s most critical documents, credentials, and post-mortem release workflows.</p>
            <div className="footer-badges">
              <span>AES-256-GCM Vault</span>
              <span>&bull;</span>
              <span>FaceNet &bull; Liveness &bull; Whisper</span>
              <span>&bull;</span>
              <span>SHA-256 Audit Trail</span>
            </div>
          </div>

          <div className="footer-links-grid">
            <div className="footer-links-col">
              <h4>Vault &amp; Platform</h4>
              <a href="#features">Features</a>
              <a href="#how-it-works">How It Works</a>
              <a href="#checkin-ai">AI Biometrics</a>
              <a href="#portals">Role Architecture</a>
            </div>

            <div className="footer-links-col">
              <h4>Portals</h4>
              <button type="button" className="footer-text-btn" onClick={() => onSignIn('login')}>Owner Vault</button>
              <button type="button" className="footer-text-btn" onClick={() => onSignIn('nominee-login')}>Nominee Portal</button>
              <button type="button" className="footer-text-btn" onClick={() => onSignIn('recipient-login')}>Beneficiary Portal</button>
              <button type="button" className="footer-text-btn" onClick={() => onSignIn('reviewer-login')}>Reviewer Portal</button>
            </div>

            <div className="footer-links-col">
              <h4>Security</h4>
              <a href="#faq">Security Architecture</a>
              <a href="#faq">Medical Hold Protocol</a>
              <a href="#faq">Zero-Knowledge Specs</a>
              <button type="button" className="footer-text-btn" onClick={onGetStarted}>Start Registration</button>
            </div>
          </div>
        </div>

        <div className="footer-bottom-bar">
          <span>&copy; {new Date().getFullYear()} CloudGuard Digital Legacy Vault. All rights reserved.</span>
          <span>End-to-End Encrypted &bull; Fail-Closed Security &bull; Privacy Preserved</span>
        </div>
      </footer>
    </div>
  )
}
