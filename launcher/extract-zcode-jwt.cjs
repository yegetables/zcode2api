#!/usr/bin/env node
/**
 * 从 ZCode 桌面端 credentials.json 解出 zcodejwttoken，写进 zcode-jwt.txt。
 *
 * 用途：NAS / 容器部署时拿不到桌面端的 ~/.zcode/v2/credentials.json，
 * 需要在有桌面端的机器上取出 JWT，填进 .env 的 ZCODE_SEED_ACCOUNTS。
 *
 * 用法：
 *   node launcher/extract-zcode-jwt.cjs                 # 写 ./zcode-jwt.txt
 *   node launcher/extract-zcode-jwt.cjs --project bigmodel
 *
 * 还原的加密方式（见 ZCode 源码 credentialCipherProvider.ts）：
 *   key  = sha256(`zcode-credential-fallback:${platform()}:${homedir()}:${username}`)
 *   密文 = enc:v1:<iv>.<authTag>.<ciphertext>，三段均 base64url，aes-256-gcm
 *
 * ⚠️ 输出的是账号凭证，等同于密码。用完请删除 zcode-jwt.txt，别提交进仓库。
 */
const { createDecipheriv, createHash } = require("node:crypto");
const { readFileSync, writeFileSync } = require("node:fs");
const { homedir, platform, userInfo } = require("node:os");
const { join } = require("node:path");

const KEY = "zcodejwttoken";
const CREDS = join(homedir(), ".zcode", "v2", "credentials.json");
const OUT = "zcode-jwt.txt";

function decrypt(value, key) {
  const [iv, tag, ct] = value.slice("enc:v1:".length).split(".");
  const d = createDecipheriv("aes-256-gcm", key, Buffer.from(iv, "base64url"));
  d.setAuthTag(Buffer.from(tag, "base64url"));
  return Buffer.concat([d.update(Buffer.from(ct, "base64url")), d.final()]).toString("utf8");
}

function main() {
  let raw;
  try {
    raw = JSON.parse(readFileSync(CREDS, "utf8"));
  } catch (err) {
    console.error(`读不到桌面端凭证: ${CREDS}\n${err.message}`);
    process.exit(1);
  }

  const key = createHash("sha256")
    .update(`zcode-credential-fallback:${platform()}:${homedir()}:${userInfo().username}`)
    .digest();

  const value = raw[KEY];
  if (!value) {
    console.error(`credentials.json 里没有 ${KEY}，先在桌面端登录。`);
    process.exit(1);
  }

  let token;
  try {
    token = value.startsWith("enc:v1:") ? decrypt(value, key) : value;
  } catch (err) {
    console.error(`解密失败（密钥与平台/用户名绑定，换机器解不开）: ${err.message}`);
    process.exit(1);
  }

  writeFileSync(OUT, token, "utf8");
  console.log(`已写出 ${OUT}（${token.length} 字符）`);
  console.log(`掩码预览: ${token.slice(0, 12)}…${token.slice(-8)}`);
  console.log("");
  console.log("填进 .env：");
  console.log('  ZCODE_SEED_ACCOUNTS="bigmodel:<把 zcode-jwt.txt 的内容粘到这里>"');
  console.log("");
  console.log("⚠️ 用完请删除 zcode-jwt.txt，且不要提交进仓库。");
}

main();
