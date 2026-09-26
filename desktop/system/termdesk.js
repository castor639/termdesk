// termdesk: a quiet first run and a steady screen for agents. Debian reads every .js in /etc/firefox-esr.
// No "security features may offer less protection" bar: Docker blocks user namespaces
pref("security.sandbox.warn_unprivileged_namespaces", false);
// No privacy notice tab, welcome tab or what's-new page
pref("datareporting.policy.dataSubmissionPolicyBypassNotification", true);
pref("browser.aboutwelcome.enabled", false);
pref("browser.startup.homepage_override.mstone", "ignore");
// No restore page or "Open previous tabs?" bar after a kill
pref("browser.sessionstore.resume_from_crash", false);
pref("browser.startup.couldRestoreSession.count", -1);
pref("browser.tabs.warnOnClose", false);
pref("browser.warnOnQuitShortcut", false);
pref("browser.aboutConfig.showWarning", false);
pref("browser.ml.chat.enabled", false);
// Nothing animates or blinks, so the screen settles and wait-idle returns
pref("general.smoothScroll", false);
pref("ui.prefersReducedMotion", 1);
pref("ui.caretBlinkTime", 0);
