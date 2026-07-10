// Costco receipt bulk export — run this in your NORMAL browser's DevTools
// Console while logged into costco.com and sitting on your receipts page.
//
// Why this exists: Costco's login is bot-protected (Kasada), so scripted logins
// get blocked. But your own browser is trusted. This snippet uses the page's own
// session (localStorage token + cookies) to fetch every receipt and download one
// JSON file, which you then feed to the tool:
//
//     python -m costco_archiver import ~/Downloads/costco_receipts.json
//     python -m costco_archiver parse && python -m costco_archiver pdf && python -m costco_archiver markdown
//
// Adjust START_YEAR if your history goes back further. It fetches one year at a
// time, newest first, dedupes by transaction barcode, and logs progress.

(async () => {
  const API = 'https://ecom-api.costco.com/ebusiness/order/v1/orders/graphql';
  const START_YEAR = 2019;

  const token = localStorage.getItem('idToken');
  const clientId = localStorage.getItem('clientID');
  if (!token || !clientId) {
    console.error('Not logged in? idToken/clientID missing from localStorage. ' +
      'Open a receipt first so the app initializes, then re-run.');
    return;
  }

  const headers = {
    'Content-Type': 'application/json-patch+json',
    'Costco-X-Authorization': 'Bearer ' + token,
    'Costco-X-Wcs-Clientid': clientId,
    'Costco.Env': 'ecom',
    'Costco.Service': 'restOrders',
    'Client-Identifier': '481b1aec-aa3b-454b-b81b-48187e28f205',
  };

  const query = `query receiptsWithCounts($startDate:String!,$endDate:String!,$documentType:String!){
    receiptsWithCounts(startDate:$startDate,endDate:$endDate,documentType:$documentType){
      receipts{
        warehouseName warehouseShortName warehouseNumber documentType
        transactionDateTime transactionDate transactionType transactionBarcode
        total subTotal taxes totalItemCount instantSavings
        itemArray{ itemNumber itemDescription01 itemDescription02 itemIdentifier
          itemDepartmentNumber unit amount taxFlag itemUnitPriceAmount }
      }
    }
  }`;

  const all = {};
  const now = new Date();
  for (let y = now.getFullYear(); y >= START_YEAR; y--) {
    const startDate = `${y}-01-01`, endDate = `${y}-12-31`;
    try {
      const resp = await fetch(API, {
        method: 'POST', headers, credentials: 'include',
        body: JSON.stringify({ query, variables: { startDate, endDate, documentType: 'all' } }),
      });
      if (!resp.ok) { console.warn(y, 'HTTP', resp.status); continue; }
      const j = await resp.json();
      if (j.errors) console.warn(y, 'GraphQL errors:', j.errors);
      const recs = (((j.data || {}).receiptsWithCounts || {}).receipts) || [];
      recs.forEach(r => { all[r.transactionBarcode || Math.random()] = r; });
      const withItems = recs.filter(r => (r.itemArray || []).length).length;
      console.log(`${y}: ${recs.length} receipts (${withItems} with line items)`);
    } catch (err) {
      console.error(y, err);
    }
    await new Promise(res => setTimeout(res, 400)); // be gentle on the API
  }

  const receipts = Object.values(all);
  console.log(`Warehouse/gas receipts: ${receipts.length}`);

  if (!receipts.length) {
    console.error('Nothing returned. Share any GraphQL error above and we can adjust.');
    return;
  }
  // `import` extracts warehouse/gas receipts from this download.
  const out = { receipts };
  const blob = new Blob([JSON.stringify(out, null, 2)], { type: 'application/json' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'costco_receipts.json';
  document.body.appendChild(a); a.click(); a.remove();
  console.log(`Downloaded costco_receipts.json (${receipts.length} receipts).`);
})();
