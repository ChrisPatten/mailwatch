# Security

mailwatch exists to keep untrusted email away from agents that can act. Bypasses of that boundary are the bugs that matter most. Examples:

- getting a URL, a tool call or another message's content into or out of the model;
- getting the broker to fetch something its rules should block (action links, private addresses, redirects);
- hiding instructions that survive normalization into model input or a record;
- getting ungrounded numbers into a record;
- making the IMAP wrapper change server state.

Please report these privately through GitHub's **Report a vulnerability** (Security tab), not in a public issue. Include a minimal `.eml`, URL or test case if you can. Adding it as a canary in `mailwatch/canaries.py` or a test is the fastest way to a fix.

Misclassification (a phishing email marked important, say) is a known limitation rather than a vulnerability. See "What it does not protect against" in the README. Ideas for better heuristics are welcome as regular issues.
