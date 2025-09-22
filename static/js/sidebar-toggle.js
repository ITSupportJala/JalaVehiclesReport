document.addEventListener('DOMContentLoaded', function () {
  const toggleBtn = document.getElementById('toggleSidebar');
  const sidebar = document.getElementById('sidebar');
  const toggleIcon = document.getElementById('toggleIcon');
  const mainContent = document.getElementById('mainContent');

  toggleBtn?.addEventListener('click', () => {
    sidebar.classList.toggle('collapsed');
    mainContent.classList.toggle('expanded');
    toggleIcon.textContent = sidebar.classList.contains('collapsed') ? '❯' : '❮';
  });
});
