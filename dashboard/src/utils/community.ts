/** The apex domain every Retina hostname hangs off. Spelled here once, so the
 *  sibling hosts in siblings.ts and the website link below cannot drift. */
export const RETINA_DOMAIN = "retina.fm";

export const DISCORD_URL = "https://discord.gg/retina";

// The apex on every environment: unlike the app and tower finder, the website
// has no staging or test sibling to derive.
export const WEBSITE_URL = `https://${RETINA_DOMAIN}`;
