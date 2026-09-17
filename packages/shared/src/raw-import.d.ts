/* Vite's raw import, which the token test uses to read the stylesheets as text
   rather than through fs — this package carries no @types/node. */
declare module "*?raw" {
  const contents: string;
  export default contents;
}
