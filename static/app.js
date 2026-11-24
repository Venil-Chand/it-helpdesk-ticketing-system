// Submit the dashboard filters as soon as a dropdown changes. This lives in
// a file because the Content Security Policy blocks inline onchange handlers.
// Without JavaScript, the Search button still submits the same form.
document.addEventListener("change", function (event) {
  var control = event.target;

  if (control.matches("select[data-auto-submit]") && control.form) {
    control.form.submit();
  }
});

// Ask before submitting a form marked data-confirm="question", for actions
// such as deactivating an account. Without JavaScript the form still works.
document.addEventListener("submit", function (event) {
  var question = event.target.getAttribute("data-confirm");

  if (question && !window.confirm(question)) {
    event.preventDefault();
  }
});
