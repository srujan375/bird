/** Artboards for the dev fixture: the podcast landing page the Open Design
 *  prototypes were drawn around, in the two directions they show. */

const EPISODES = [["Ep 1 — Cold open", "48 min"], ["Ep 2 — The seam", "51 min"], ["Ep 3 — One document", "44 min"]];

const rows = (font: string) => EPISODES.map(([t, m], i) => `
      <div style="display:flex;justify-content:space-between;align-items:baseline;padding:14px 0;${i < 2 ? "border-bottom:1px solid oklch(93% 0.004 250);" : ""}">
        <span style="font-family:${font};font-size:18px;color:oklch(20% 0.01 250)">${t}</span>
        <span style="font-size:14px;color:oklch(55% 0.01 250)">${m}</span>
      </div>`).join("");

export const DARK = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Signal &amp; Noise</title>
</head>
<body style="margin:0;background:#fff;font-family:-apple-system,BlinkMacSystemFont,'SF Pro Text',system-ui,sans-serif;color:#111">
  <header class="hero" style="background:#101418;padding:72px 64px 64px">
    <p style="margin:0 0 18px;font-family:ui-monospace,'SF Mono',Menlo,monospace;font-size:12px;letter-spacing:.12em;text-transform:uppercase;color:#7c8794">A podcast about building software in the open</p>
    <h1 style="margin:0;font-family:-apple-system,BlinkMacSystemFont,'SF Pro Display',system-ui,sans-serif;font-size:64px;font-weight:700;letter-spacing:-.025em;line-height:1.05;color:#eef1f7">Signal &amp; Noise</h1>
    <p style="margin:22px 0 0;font-size:20px;line-height:1.5;color:#a8b0bd;max-width:44ch">Weekly &amp; unfiltered. Two engineers, one long conversation about the work nobody writes up.</p>
    <div style="display:flex;gap:12px;margin-top:36px">
      <a href="#" style="display:inline-block;padding:14px 22px;border-radius:10px;background:#eef1f7;color:#101418;font-weight:600;font-size:15px;text-decoration:none">Listen to the latest</a>
      <a href="#" style="display:inline-block;padding:14px 22px;border-radius:10px;border:1px solid #2a323b;color:#eef1f7;font-weight:500;font-size:15px;text-decoration:none">Subscribe</a>
    </div>
  </header>
  <main style="padding:48px 64px 72px;max-width:760px">
    <p style="margin:0 0 10px;font-size:12px;letter-spacing:.1em;text-transform:uppercase;color:oklch(58% 0.01 250);font-weight:600">Latest episodes</p>
    <div>${rows("-apple-system,BlinkMacSystemFont,'SF Pro Display',system-ui,sans-serif")}
    </div>
    <p style="margin:40px 0 0;font-size:15px;line-height:1.6;color:oklch(45% 0.01 250)">New episodes every Thursday. Transcripts, show notes and the code we talk about are on the episode pages.</p>
  </main>
</body>
</html>`;

export const DARK_WARMER = DARK
  .replace("font-size:64px;font-weight:700;letter-spacing:-.025em", "font-family:Georgia,'Times New Roman',serif;font-size:66px;font-weight:400;letter-spacing:-.02em")
  .replace("Weekly &amp; unfiltered. Two engineers", "Weekly and unfiltered. Two engineers");

export const WARM = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Signal</title>
</head>
<body style="margin:0;background:#faf7f2;font-family:Georgia,'Times New Roman',serif;color:#2b2419">
  <header class="hero" style="background:#e8dcc8;padding:80px 64px 60px">
    <h1 style="margin:0;font-size:72px;font-weight:400;letter-spacing:-.02em;line-height:1.02;color:#2b2419">Signal</h1>
    <p style="margin:20px 0 0;font-family:-apple-system,BlinkMacSystemFont,'SF Pro Text',system-ui,sans-serif;font-size:19px;line-height:1.5;color:#6b5f4c;max-width:40ch">Weekly &amp; unfiltered. A slow conversation about software, recorded on Thursdays.</p>
    <a href="#" style="display:inline-block;margin-top:34px;padding:13px 20px;border-radius:999px;background:#2b2419;color:#faf7f2;font-family:-apple-system,BlinkMacSystemFont,'SF Pro Text',system-ui,sans-serif;font-size:15px;font-weight:500;text-decoration:none">Start with episode one</a>
  </header>
  <main style="padding:48px 64px 72px;max-width:720px">
    <p style="margin:0 0 10px;font-family:-apple-system,BlinkMacSystemFont,'SF Pro Text',system-ui,sans-serif;font-size:12px;letter-spacing:.1em;text-transform:uppercase;color:oklch(58% 0.01 250);font-weight:600">Latest episodes</p>
    <div>${rows("Georgia,'Times New Roman',serif")}
    </div>
  </main>
</body>
</html>`;

export const EPISODE = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Ep 1 — Cold open</title>
</head>
<body style="margin:0;background:#fff;font-family:-apple-system,BlinkMacSystemFont,'SF Pro Text',system-ui,sans-serif;color:#111">
  <header style="background:#101418;padding:44px 64px 40px">
    <p style="margin:0 0 10px;font-family:ui-monospace,'SF Mono',Menlo,monospace;font-size:12px;letter-spacing:.12em;text-transform:uppercase;color:#7c8794">Episode 1 · 48 min</p>
    <h1 style="margin:0;font-family:-apple-system,BlinkMacSystemFont,'SF Pro Display',system-ui,sans-serif;font-size:40px;font-weight:700;letter-spacing:-.02em;line-height:1.15;color:#eef1f7">Cold open</h1>
  </header>
  <main style="padding:40px 64px 72px;max-width:680px;font-size:17px;line-height:1.65;color:oklch(30% 0.01 250)">
    <p style="margin:0 0 18px">We start where every project starts: with a blank repo and too many opinions. What to decide first, what to leave undecided, and why the first week is mostly arguments about names.</p>
    <p style="margin:0 0 18px">Along the way: the one document we keep coming back to, the seam between design and code, and a confession about a rewrite that should not have happened.</p>
    <a href="#" style="display:inline-block;margin-top:8px;padding:12px 18px;border-radius:9px;background:#101418;color:#eef1f7;font-weight:600;font-size:14px;text-decoration:none">Play episode</a>
  </main>
</body>
</html>`;
