// Click a column heading to sort by it, on every table[data-sortable] on the page.
(() => {
  // Sort on data-v when present, else the cell text.
  const value = (cell) => cell.dataset.v === undefined ? cell.textContent.trim() : cell.dataset.v;
  // Keys that sort ascending on first click; the rest start descending.
  const ascendingFirst = ['name', 'house', 'strategy', 'manager', 'ter', 'ber', 'bench'];

  document.querySelectorAll('table[data-sortable]').forEach((table) => {
    const body = table.tBodies[0];
    const heads = [...table.tHead.rows[0].cells];

    function sortBy(i, desc) {
      const rows = [...body.rows];
      rows.sort((a, b) => {
        const x = value(a.cells[i]), y = value(b.cells[i]);
        // Blank values sort last in both directions.
        if (!x || !y) return (!x) === (!y) ? 0 : (x ? -1 : 1);
        const nx = parseFloat(x.replace(/,/g, '')), ny = parseFloat(y.replace(/,/g, ''));
        const cmp = Number.isNaN(nx) || Number.isNaN(ny) ? x.localeCompare(y) : nx - ny;
        return desc ? -cmp : cmp;
      });
      body.append(...rows);
      heads.forEach((h, j) => h.setAttribute('aria-sort', j === i ? (desc ? 'descending' : 'ascending') : 'none'));
    }

    heads.forEach((h, i) => {
      const key = h.dataset.key;
      if (!key) return;
      h.tabIndex = 0;
      h.setAttribute('role', 'button');
      h.setAttribute('aria-sort', 'none');
      h.classList.add('sortable');
      let desc = !ascendingFirst.includes(key);
      const go = () => { sortBy(i, desc); desc = !desc; };
      h.addEventListener('click', go);
      h.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); go(); }
      });
    });
  });
})();
