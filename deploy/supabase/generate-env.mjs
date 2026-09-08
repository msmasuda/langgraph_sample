import { createHash, createHmac, generateKeyPairSync, randomBytes, randomUUID, sign } from "node:crypto";
import { readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const directory = dirname(fileURLToPath(import.meta.url));
const template = readFileSync(join(directory, ".env.example"), "utf8");
const base64url = (value) => Buffer.from(value).toString("base64url");
const jwtSecret = randomBytes(30).toString("base64");
const issuedAt = Math.floor(Date.now() / 1000);
const expiresAt = issuedAt + 5 * 365 * 24 * 60 * 60;

function jwt(payload, key, algorithm, kid) {
  const header = base64url(JSON.stringify({ alg: algorithm, typ: "JWT", ...(kid && { kid }) }));
  const body = base64url(JSON.stringify(payload));
  const data = `${header}.${body}`;
  const signature = algorithm === "HS256"
    ? createHmac("sha256", key).update(data).digest("base64url")
    : sign("sha256", Buffer.from(data), { key, dsaEncoding: "ieee-p1363" }).toString("base64url");
  return `${data}.${signature}`;
}

function opaque(prefix) {
  const body = `${prefix}${randomBytes(17).toString("base64url").slice(0, 22)}`;
  const checksum = createHash("sha256")
    .update(`supabase-self-hosted|${body}`)
    .digest("base64url")
    .slice(0, 8);
  return `${body}_${checksum}`;
}

const legacy = (role) => jwt({ role, iss: "supabase", iat: issuedAt, exp: expiresAt }, jwtSecret, "HS256");
const { privateKey } = generateKeyPairSync("ec", { namedCurve: "P-256" });
const privateJwk = privateKey.export({ format: "jwk" });
const kid = randomUUID();
const ecPublic = {
  kty: "EC", kid, use: "sig", key_ops: ["verify"], alg: "ES256", ext: true,
  crv: privateJwk.crv, x: privateJwk.x, y: privateJwk.y,
};
const ecPrivate = { ...ecPublic, key_ops: ["sign", "verify"], d: privateJwk.d };
const oct = { kty: "oct", k: base64url(jwtSecret), alg: "HS256" };
const asymmetric = (role) => jwt({ role, iss: "supabase", iat: issuedAt, exp: expiresAt }, privateKey, "ES256", kid);

const values = [
  randomBytes(16).toString("hex"),
  jwtSecret,
  legacy("anon"),
  legacy("service_role"),
  opaque("sb_publishable_"),
  opaque("sb_secret_"),
  asymmetric("anon"),
  asymmetric("service_role"),
  JSON.stringify([ecPrivate, oct]),
  randomBytes(24).toString("base64"),
  randomBytes(16).toString("hex"),
];
let index = 0;
if ((template.match(/GENERATE_ME/g) ?? []).length !== values.length) {
  throw new Error(".env.exampleの生成対象数が一致しません。");
}
const output = template.replaceAll("GENERATE_ME", () => values[index++]);
if (process.argv.includes("--stdout")) {
  process.stdout.write(output);
} else {
  writeFileSync(join(directory, ".env"), output, { encoding: "utf8", flag: "wx", mode: 0o600 });
  console.log("deploy/supabase/.env を生成しました。");
}
