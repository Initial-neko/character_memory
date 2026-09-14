(() => {
  const CM = window.CM;
  if (!CM) throw new Error("CM core must load before time_format.js");
  const pad = value => String(value).padStart(2, "0");
  CM.fmtTime = iso => {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return "";
    return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  };
})();