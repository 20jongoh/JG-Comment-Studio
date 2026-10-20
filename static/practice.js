document.querySelector('#save').addEventListener('click', () => {
  const value = document.querySelector('#editor').value;
  if (!value.trim()) return;
  document.querySelector('[data-testid="practice-saved"]').textContent = value;
  document.querySelector('#result').hidden = false;
});
