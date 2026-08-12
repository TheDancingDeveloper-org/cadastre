// The build-time default: talk to whatever origin served the page.
//
// Copied verbatim into dist/ as a public asset, never bundled, so a
// deployment can replace it without a rebuild. The GUI container does
// exactly that — runtime-config.template.js is rendered at start and
// served in place of this file (Dockerfile.gui, ui/nginx.conf).
window.__CADASTRE_CONFIG__ = window.__CADASTRE_CONFIG__ || { apiOrigin: "" };
