(function (downloadName) {
  const cookies = document.cookie.split("; ");
  const domain = window.location.hostname;
  const isSecure = window.location.protocol === "https:" ? "TRUE" : "FALSE";

  const lines = [
    "# Netscape HTTP Cookie File",
    "# http://curl.haxx.se/rfc/cookie_spec.html",
    "# This is a generated file! Do not edit.",
    "",
  ];

  for (const cookie of cookies) {
    const [name, value] = cookie.split("=", 2);
    const row = [`.${domain}`, "TRUE", "/", isSecure, 0, name, value];
    lines.push(row.join("\t"));
  }

  const blob = new Blob([lines.join("\n")], { type: "text/plain" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = downloadName;
  link.click();
  URL.revokeObjectURL(url);
})("cookies.txt");
