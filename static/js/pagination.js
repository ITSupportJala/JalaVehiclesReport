function goToPage(page) {
  document.getElementById('pageInput').value = page;
  document.querySelector('form').submit();
}

function jumpToPage(maxPage) {
  const input = document.getElementById('jumpPageInput');
  let page = parseInt(input.value);

  if (isNaN(page) || page < 1) {
    page = 1;
  } else if (page > maxPage) {
    page = maxPage;
  }

  goToPage(page);
}
