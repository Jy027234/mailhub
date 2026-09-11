/**
 * The browser accessibility audit loads the shipped stylesheet so that contrast,
 * target size and reflow are measured against real painted pixels.  Vite
 * resolves the import; this declaration only tells TypeScript that it is legal.
 */
declare module "*.css";
