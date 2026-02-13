const crypto = require("crypto");
const { marked } = require("marked");
const puppeteer = require("puppeteer-extra");
const StealthPlugin = require("puppeteer-extra-plugin-stealth");
const { ProxyAgent } = require("undici");

puppeteer.use(StealthPlugin());

// Browser launch semaphore — prevents OOM from too many Chrome instances at once
const MAX_CONCURRENT_BROWSERS = 3;
let activeBrowsers = 0;
const browserQueue = [];

function acquireBrowserSlot() {
  return new Promise((resolve) => {
    if (activeBrowsers < MAX_CONCURRENT_BROWSERS) {
      activeBrowsers++;
      resolve();
    } else {
      browserQueue.push(resolve);
    }
  });
}

function releaseBrowserSlot() {
  if (browserQueue.length > 0) {
    const next = browserQueue.shift();
    next();
  } else {
    activeBrowsers--;
  }
}

// Realistic User-Agent pool — each pool client gets a distinct one (15+ entries)
const USER_AGENTS = [
  // Chrome — Windows
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
  // Chrome — Mac
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
  // Chrome — Linux
  "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
  "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
  // Firefox — Windows
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:132.0) Gecko/20100101 Firefox/132.0",
  // Firefox — Mac
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:133.0) Gecko/20100101 Firefox/133.0",
  // Firefox — Linux
  "Mozilla/5.0 (X11; Linux x86_64; rv:133.0) Gecko/20100101 Firefox/133.0",
  // Edge — Windows
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0",
  // Edge — Mac
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
  // Safari — Mac
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.2 Safari/605.1.15",
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Safari/605.1.15",
];

// System prompt to instruct Meta AI to return structured JSON
const SYSTEM_PROMPT = `You are an API backend model that must always return responses in a strict JSON schema.
Your goal is to produce comprehensive, deeply informative, and structured content — at least several paragraphs long — while respecting the format rules below.

When given a user query:
1. Produce a long, detailed answer with clear explanations, comparisons, and examples.
2. Include both:
   - A markdown version (formatted with headers, bold, lists, tables, etc.)
   - A plain text version (identical content but without markdown formatting)
3. Include at least 3 to 7 credible sources, each with:
   - position (integer starting at 0)
   - label (title or entity name)
   - url (credible or official site)
   - description (short summary of the source)
4. Include 3 to 6 search queries that could help someone find this answer online.
5. Include the model used in format "model": "meta-ai".
6. Return nothing outside the JSON — no commentary or extra lines.

Your output must always follow this structure:
{
  "success": true,
  "result": {
    "markdown": "string",
    "text": "string",
    "sources": [
      {
        "position": "number",
        "label": "string",
        "url": "string",
        "description": "string"
      }
    ],
    "searchQueries": ["string"],
    "model": "string"
  }
}

### Additional style and length requirements:
- The answer should be at least 250-400 words long.
- Use factual, neutral, and informative tone.
- Markdown version should include:
  - A bolded introductory sentence
  - Bullet points or numbered lists when relevant
  - Subheadings for structure
- Plain text version should preserve the same logical flow but without markdown syntax.

If information is missing, return an empty string or empty array instead of omitting fields.
No explanations or reasoning outside the JSON are allowed.`;

/**
 * Generate an offline threading ID matching Meta's format.
 */
function generateOfflineThreadingId() {
  const maxInt = BigInt((1n << 64n) - 1n);
  const mask22 = BigInt((1n << 22n) - 1n);
  const timestamp = BigInt(Date.now());
  const randomValue = BigInt(`0x${crypto.randomBytes(8).toString("hex")}`);
  const shifted = timestamp << 22n;
  const masked = randomValue & mask22;
  return String((shifted | masked) & maxInt);
}

/**
 * Extract a value from text between start_str and end_str.
 */
function extractValue(text, startStr, endStr) {
  const start = text.indexOf(startStr);
  if (start === -1) return "";
  const valueStart = start + startStr.length;
  const end = text.indexOf(endStr, valueStart);
  if (end === -1) return "";
  return text.substring(valueStart, end);
}

class MetaAIClient {
  constructor(proxy = null, id = null) {
    this.proxy = proxy;
    this.proxyDispatcher = proxy ? new ProxyAgent(proxy) : undefined;
    // Parse proxy URL for Puppeteer (Chrome needs host:port separately from credentials)
    if (proxy) {
      const parsed = new URL(proxy);
      this.proxyServer = `${parsed.protocol}//${parsed.hostname}:${parsed.port}`;
      this.proxyAuth = parsed.username
        ? { username: decodeURIComponent(parsed.username), password: decodeURIComponent(parsed.password) }
        : null;
    } else {
      this.proxyServer = null;
      this.proxyAuth = null;
    }
    this.id = id;
    this.logPrefix = id != null ? `[meta-client:${id}]` : "[meta-client]";
    this.cookies = null;
    this.accessToken = null;
    this.userAgent = id != null
      ? USER_AGENTS[id % USER_AGENTS.length]
      : USER_AGENTS[Math.floor(Math.random() * USER_AGENTS.length)];
  }

  /**
   * Fetch cookies from the Meta AI homepage using a headless browser.
   * Meta AI uses TLS fingerprinting/bot detection that blocks plain HTTP clients.
   */
  async getCookies() {
    await acquireBrowserSlot();
    console.log(`${this.logPrefix} Launching browser to extract cookies... (active: ${activeBrowsers}/${MAX_CONCURRENT_BROWSERS})`);

    let browser;
    try {
      browser = await puppeteer.launch({
        headless: "new",
        executablePath:
          process.env.CHROME_PATH ||
          "C:/Program Files/Google/Chrome/Application/chrome.exe",
        args: [
          "--no-sandbox",
          "--disable-setuid-sandbox",
          "--disable-blink-features=AutomationControlled",
          "--disable-infobars",
          "--window-size=1920,1080",
          "--start-maximized",
          "--disable-dev-shm-usage",
          "--disable-gpu",
          "--disable-extensions",
          "--disable-background-networking",
          "--disable-default-apps",
          "--disable-translate",
          "--no-first-run",
          "--lang=en-US,en",
          ...(this.proxyServer ? [`--proxy-server=${this.proxyServer}`] : []),
        ],
      });

      const page = await browser.newPage();
      if (this.proxyAuth) {
        await page.authenticate(this.proxyAuth);
      }
      await page.setUserAgent(this.userAgent);
      await page.setViewport({ width: 1920, height: 1080 });

      // Block heavy resources to save proxy bandwidth
      await page.setRequestInterception(true);
      page.on("request", (req) => {
        const type = req.resourceType();
        if (["image", "font", "stylesheet", "media", "texttrack", "manifest"].includes(type)) {
          req.abort();
        } else {
          req.continue();
        }
      });

      // Set realistic browser properties
      await page.evaluateOnNewDocument(() => {
        Object.defineProperty(navigator, "languages", {
          get: () => ["en-US", "en"],
        });
        Object.defineProperty(navigator, "platform", {
          get: () => "Win32",
        });
      });

      // Navigate — catch all navigation errors (redirects, context destruction)
      // and wait for the page to settle regardless
      try {
        await page.goto("https://www.meta.ai/", {
          waitUntil: "load",
          timeout: 90000,
        });
      } catch (navErr) {
        const msg = navErr.message || "";
        if (!msg.includes("Execution context") && !msg.includes("navigation")) throw navErr;
        console.log(`${this.logPrefix} Redirect detected, waiting for page to settle...`);
      }

      // Wait for the page to have real content (JS needs to populate the React shell)
      try {
        await page.waitForFunction(
          () => document.documentElement.innerHTML.length > 10000,
          { timeout: 60000 }
        );
      } catch {
        console.log(`${this.logPrefix} Page content still small after 60s wait`);
      }

      // Log where we actually ended up
      const currentUrl = page.url();
      console.log(`${this.logPrefix} Landed on: ${currentUrl}`);

      // Extract tokens from the page HTML
      const html = await page.content();

      // Debug: log page title and HTML snippet to diagnose proxy issues
      const title = html.match(/<title[^>]*>(.*?)<\/title>/i)?.[1] || "(no title)";
      console.log(`${this.logPrefix} Page title: "${title}" | HTML length: ${html.length} | Has LSD: ${html.includes('"LSD"')}`);

      this.cookies = {
        _js_datr: extractValue(html, '_js_datr":{"value":"', '",'),
        abra_csrf: extractValue(html, 'abra_csrf":{"value":"', '",'),
        datr: extractValue(html, 'datr":{"value":"', '",'),
        lsd: extractValue(html, '"LSD",[],{"token":"', '"}'),
      };

      // Extract fb_dtsg if available
      const fbDtsg = extractValue(html, '"DTSGInitData",[],{"token":"', '"');
      if (fbDtsg) {
        this.cookies.fb_dtsg = fbDtsg;
      }

      // Also grab browser cookies
      const browserCookies = await page.cookies();
      for (const c of browserCookies) {
        if (c.name === "_js_datr" && !this.cookies._js_datr) {
          this.cookies._js_datr = c.value;
        }
        if (c.name === "datr" && !this.cookies.datr) {
          this.cookies.datr = c.value;
        }
        if (c.name === "abra_csrf" && !this.cookies.abra_csrf) {
          this.cookies.abra_csrf = c.value;
        }
      }

      if (!this.cookies.lsd) {
        throw new Error(
          "Failed to extract LSD token from Meta AI homepage"
        );
      }

      console.log(
        `${this.logPrefix} Extracted cookies:`,
        Object.fromEntries(
          Object.entries(this.cookies).map(([k, v]) => [
            k,
            v ? v.substring(0, 10) + "..." : "(empty)",
          ])
        )
      );

      return this.cookies;
    } finally {
      try {
        await browser.close();
      } catch { /* ignore */ }
      // Force-kill Chrome process in case browser.close() left orphans
      try {
        const proc = browser.process();
        if (proc && !proc.killed) proc.kill("SIGKILL");
      } catch { /* ignore */ }
      releaseBrowserSlot();
    }
  }

  /**
   * Get an anonymous access token by accepting TOS as a temp user.
   */
  async getAccessToken() {
    if (!this.cookies) {
      await this.getCookies();
    }

    const url = "https://www.meta.ai/api/graphql/";

    const cookieStr = [
      `_js_datr=${this.cookies._js_datr}`,
      `datr=${this.cookies.datr}`,
      `abra_csrf=${this.cookies.abra_csrf}`,
      `ps_n=1`,
      `ps_l=1`,
      `dpr=2`,
    ].join("; ");

    const body = new URLSearchParams({
      lsd: this.cookies.lsd,
      fb_dtsg: this.cookies.fb_dtsg || "",
      fb_api_caller_class: "RelayModern",
      fb_api_req_friendly_name: "useAbraAcceptTOSForTempUserMutation",
      variables: JSON.stringify({ dob: "1999-01-01", tos_accepted: true }),
      doc_id: "7604648749596940",
    });

    const resp = await fetch(url, {
      method: "POST",
      ...(this.proxyDispatcher && { dispatcher: this.proxyDispatcher }),
      headers: {
        "User-Agent": this.userAgent,
        Accept: "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Content-Type": "application/x-www-form-urlencoded",
        Origin: "https://www.meta.ai",
        Referer: "https://www.meta.ai/",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        Cookie: cookieStr,
        "X-Fb-Friendly-Name": "useAbraAcceptTOSForTempUserMutation",
        "X-Fb-Lsd": this.cookies.lsd,
        "X-Asbd-Id": "129477",
      },
      body: body.toString(),
    });

    if (!resp.ok) {
      throw new Error(`Token request failed: ${resp.status} ${await resp.text()}`);
    }

    const text = await resp.text();

    // Response may have multiple JSON objects on separate lines
    let data = null;
    for (const line of text.split("\n")) {
      const trimmed = line.trim();
      if (!trimmed) continue;
      try {
        const parsed = JSON.parse(trimmed);
        if (parsed.data) {
          data = parsed;
          break;
        }
      } catch {
        continue;
      }
    }

    if (!data) {
      throw new Error(`Failed to parse access token response: ${text.substring(0, 500)}`);
    }

    try {
      this.accessToken =
        data.data.xab_abra_accept_terms_of_service.new_temp_user_auth
          .access_token;
    } catch (e) {
      throw new Error(
        `Failed to extract access token: ${e.message}\nResponse: ${JSON.stringify(data).substring(0, 500)}`
      );
    }

    console.log(
      `${this.logPrefix} Got access token: ${this.accessToken.substring(0, 20)}...`
    );
    return this.accessToken;
  }

  /**
   * Ensure we have valid cookies and access token. Retries once on failure.
   */
  async ensureSession() {
    try {
      if (!this.cookies) await this.getCookies();
      if (!this.accessToken) await this.getAccessToken();
    } catch (err) {
      console.log(`${this.logPrefix} Session setup failed, retrying:`, err.message);
      this.resetSession();
      await this.getCookies();
      await this.getAccessToken();
    }
  }

  /**
   * Fire a single GraphQL sendMessage call and return the raw response text.
   * Does NOT handle retries or session refresh — that's sendMessage's job.
   */
  async _fireMessage(prompt) {
    const url = "https://graph.meta.ai/graphql?locale=user";
    const externalConversationId = crypto.randomUUID();
    const offlineThreadingId = generateOfflineThreadingId();

    const variables = {
      message: { sensitive_string_value: prompt },
      externalConversationId,
      offlineThreadingId,
      suggestedPromptIndex: null,
      flashVideoRecapInput: { images: [] },
      flashPreviewInput: null,
      promptPrefix: null,
      entrypoint: "ABRA__CHAT__TEXT",
      icebreaker_type: "TEXT",
    };

    const body = new URLSearchParams({
      fb_api_caller_class: "RelayModern",
      fb_api_req_friendly_name: "useAbraSendMessageMutation",
      variables: JSON.stringify(variables),
      server_timestamps: "true",
      doc_id: "7783822248314888",
    });

    const resp = await fetch(url, {
      method: "POST",
      ...(this.proxyDispatcher && { dispatcher: this.proxyDispatcher }),
      headers: {
        "User-Agent": this.userAgent,
        Accept: "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Content-Type": "application/x-www-form-urlencoded",
        Origin: "https://www.meta.ai",
        Referer: "https://www.meta.ai/",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-site",
        Authorization: `OAuth ${this.accessToken}`,
      },
      body: body.toString(),
    });

    if (!resp.ok) {
      const errText = await resp.text();
      throw new Error(`Send message failed: ${resp.status} ${errText.substring(0, 500)}`);
    }

    return resp.text();
  }

  /**
   * Check if a raw GraphQL response indicates an exhausted/invalid session.
   * Meta AI returns a tiny ~535 byte response with "missing_required_variable_value"
   * when the temp user token is burned out.
   */
  _isSessionExhausted(responseText) {
    return (
      responseText.length < 1000 &&
      (responseText.includes("missing_required_variable_value") ||
        responseText.includes('"bot_response_message":null'))
    );
  }

  /**
   * Send a message to Meta AI and return the parsed response.
   * If the session is exhausted, automatically refreshes and retries once.
   */
  async sendMessage(prompt) {
    await this.ensureSession();

    let responseText = await this._fireMessage(prompt);

    // Detect exhausted token — refresh session and retry once
    if (this._isSessionExhausted(responseText)) {
      console.log(`${this.logPrefix} Session exhausted, refreshing token and retrying...`);
      this.resetSession();
      await this.ensureSession();
      responseText = await this._fireMessage(prompt);

      if (this._isSessionExhausted(responseText)) {
        throw new Error("Session exhausted even after refresh");
      }
    }

    // Debug: log raw response size
    console.log(
      `${this.logPrefix} Raw response: ${responseText.length} bytes, ` +
      `lines: ${responseText.split("\n").filter(l => l.trim()).length}`
    );

    const parsed = this.parseResponse(responseText);

    if (!parsed.text) {
      console.log(`${this.logPrefix} WARNING: Empty parsed text. Raw preview:`);
      console.log(responseText.substring(0, 1000));
    }

    // Fetch real source URLs if a fetch_id is available
    if (parsed.fetchId) {
      try {
        const sources = await this.fetchSources(parsed.fetchId);
        if (sources.length > 0) {
          parsed.rawSources = sources;
        }
      } catch (err) {
        console.log(`${this.logPrefix} Failed to fetch sources:`, err.message);
      }
    }

    return parsed;
  }

  /**
   * Fetch real source URLs using the fetch_id from a response.
   */
  async fetchSources(fetchId) {
    const url = "https://graph.meta.ai/graphql?locale=user";

    const body = new URLSearchParams({
      access_token: this.accessToken,
      fb_api_caller_class: "RelayModern",
      fb_api_req_friendly_name: "AbraSearchPluginDialogQuery",
      variables: JSON.stringify({ abraMessageFetchID: fetchId }),
      server_timestamps: "true",
      doc_id: "6946734308765963",
    });

    const resp = await fetch(url, {
      method: "POST",
      ...(this.proxyDispatcher && { dispatcher: this.proxyDispatcher }),
      headers: {
        "User-Agent": this.userAgent,
        Accept: "*/*",
        "Content-Type": "application/x-www-form-urlencoded",
        Origin: "https://www.meta.ai",
        Referer: "https://www.meta.ai/",
        Authorization: `OAuth ${this.accessToken}`,
      },
      body: body.toString(),
    });

    if (!resp.ok) {
      throw new Error(`Fetch sources failed: ${resp.status}`);
    }

    const data = await resp.json();
    const references =
      data?.data?.message?.searchResults?.references ||
      data?.data?.message?.search_results?.references ||
      [];

    return references.map((ref) => ({
      url: ref.url || ref.link || "",
      label: ref.title || ref.name || "",
      description: ref.snippet || ref.description || "",
    }));
  }

  /**
   * Parse the NDJSON streaming response from Meta AI.
   */
  parseResponse(responseText) {
    let lastValidResponse = null;
    const rawSources = [];
    let fetchId = null;

    for (const line of responseText.split("\n")) {
      const trimmed = line.trim();
      if (!trimmed) continue;

      let data;
      try {
        data = JSON.parse(trimmed);
      } catch {
        continue;
      }

      // Extract bot response message from streamed or initial response
      const botMsg =
        data?.data?.node?.bot_response_message ||
        data?.data?.xfb_abra_send_message?.bot_response_message;

      if (botMsg) {
        const contentList = botMsg.composed_text?.content || [];
        if (contentList.length > 0) {
          lastValidResponse = botMsg;
        }

        if (botMsg.fetch_id) {
          fetchId = botMsg.fetch_id;
        }
      }

      // Look for source references
      const searchResults =
        data?.data?.node?.search_results ||
        data?.data?.xfb_abra_send_message?.bot_response_message?.search_results;
      if (searchResults) {
        const references = searchResults.references || [];
        for (const ref of references) {
          rawSources.push({
            url: ref.url || "",
            label: ref.title || "",
            description: ref.snippet || "",
          });
        }
      }
    }

    if (!lastValidResponse) {
      return { text: "", rawSources: [], fetchId: null };
    }

    // Build full text from composed_text.content
    const textParts = (lastValidResponse.composed_text?.content || []).map(
      (c) => c.text || ""
    );
    const fullText = textParts.join("\n");

    // Extract inline sources if no structured sources found
    if (rawSources.length === 0) {
      const inlineSources = this.extractInlineSources(fullText);
      rawSources.push(...inlineSources);
    }

    return { text: fullText, rawSources, fetchId };
  }

  /**
   * Extract source URLs from Meta AI's inline source references.
   */
  extractInlineSources(text) {
    const sources = [];
    const pattern = /https?:\/\/l\.meta\.ai\/\?u=([^&\s]+)/g;
    let match;
    while ((match = pattern.exec(text)) !== null) {
      const decodedUrl = decodeURIComponent(match[1]);
      sources.push({
        url: decodedUrl,
        label: new URL(decodedUrl).hostname,
        description: "",
      });
    }
    return sources;
  }

  /**
   * Send a prompt to Meta AI and return a structured response.
   * Sends without system prompt to ensure real source URLs are returned,
   * then structures the response server-side.
   */
  async prompt(text) {
    const rawResponse = await this.sendMessage(text);
    const responseText = rawResponse.text || "";
    const rawSources = rawResponse.rawSources || [];

    return this.buildStructuredResponse(responseText, rawSources);
  }

  /**
   * Try to parse Meta AI's response as structured JSON.
   */
  tryParseJsonResponse(text) {
    // Try multiple strategies to extract JSON from the response

    // Strategy 1: Try the whole text (with code block removal)
    let jsonText = text.trim();
    if (jsonText.startsWith("```json")) jsonText = jsonText.slice(7);
    else if (jsonText.startsWith("```")) jsonText = jsonText.slice(3);
    if (jsonText.endsWith("```")) jsonText = jsonText.slice(0, -3);
    jsonText = jsonText.trim();

    const parsed = this._tryParseJson(jsonText);
    if (parsed) return parsed;

    // Strategy 2: Find JSON object by matching braces
    const firstBrace = text.indexOf("{");
    if (firstBrace !== -1) {
      let depth = 0;
      let lastBrace = -1;
      for (let i = firstBrace; i < text.length; i++) {
        if (text[i] === "{") depth++;
        else if (text[i] === "}") {
          depth--;
          if (depth === 0) {
            lastBrace = i;
            // Don't break - find the largest valid JSON object
          }
        }
      }
      // Try from the last closing brace back to the first opening brace
      if (lastBrace !== -1) {
        const extracted = text.substring(firstBrace, lastBrace + 1);
        const parsed2 = this._tryParseJson(extracted);
        if (parsed2) return parsed2;
      }
    }

    return null;
  }

  /**
   * Attempt to parse a JSON string into the expected response format.
   */
  _tryParseJson(jsonText) {
    // Try parsing directly first, then with newline fixing
    for (const text of [jsonText, this._fixJsonNewlines(jsonText)]) {
      try {
        return this._parseAndNormalize(JSON.parse(text));
      } catch {
        continue;
      }
    }
    return null;
  }

  /**
   * Fix literal newlines inside JSON string values that make it invalid.
   */
  _fixJsonNewlines(text) {
    // Replace literal newlines that appear inside JSON string values
    // This is a common issue when LLMs produce JSON with actual newlines
    let result = "";
    let inString = false;
    let escape = false;

    for (let i = 0; i < text.length; i++) {
      const ch = text[i];

      if (escape) {
        result += ch;
        escape = false;
        continue;
      }

      if (ch === "\\") {
        result += ch;
        escape = true;
        continue;
      }

      if (ch === '"') {
        inString = !inString;
        result += ch;
        continue;
      }

      if (inString && ch === "\n") {
        result += "\\n";
        continue;
      }

      if (inString && ch === "\r") {
        continue; // skip \r
      }

      result += ch;
    }

    return result;
  }

  /**
   * Normalize parsed JSON data into the expected response format.
   */
  _parseAndNormalize(data) {
    if (!data || typeof data !== "object") return null;

    // Normalize sources: if sources is array of strings, convert to objects
    const normalizeSources = (sources) => {
      if (!Array.isArray(sources)) return [];
      return sources.map((s, i) => {
        if (typeof s === "string") {
          return { position: i, url: s, label: s, description: "" };
        }
        return { position: s.position ?? i, ...s };
      });
    };

    const result = data.result || data;
    if (result.text || result.markdown) {
      return {
        success: true,
        result: {
          text: result.text || "",
          sources: normalizeSources(result.sources),
          html: result.html || "",
          markdown: result.markdown || "",
          searchQueries: result.searchQueries || [],
          shoppingCards: result.shoppingCards || [],
          model: result.model || "meta-ai",
        },
      };
    }

    return null;
  }

  /**
   * Build the structured response from raw text (fallback).
   */
  buildStructuredResponse(text, rawSources) {
    const cleanText = text.trim();
    const markdownText = cleanText;
    const htmlText = `<div class="markdown">${marked(markdownText)}</div>`;

    const sources = rawSources.map((src, i) => ({
      position: i,
      url: src.url || "",
      label: src.label || "",
      description: src.description || "",
    }));

    return {
      success: true,
      result: {
        text: cleanText,
        sources,
        html: htmlText,
        markdown: markdownText,
        searchQueries: [],
        shoppingCards: [],
        model: "meta-ai",
      },
    };
  }

  /**
   * Reset session to force re-authentication.
   */
  resetSession() {
    this.cookies = null;
    this.accessToken = null;
  }
}

module.exports = { MetaAIClient };
