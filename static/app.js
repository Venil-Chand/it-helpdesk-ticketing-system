// Submit the dashboard filters as soon as a dropdown changes. This lives in
// a file because the Content Security Policy blocks inline onchange handlers.
// Without JavaScript, the Search button still submits the same form.
document.addEventListener("change", function (event) {
  var control = event.target;

  if (control.matches("select[data-auto-submit]") && control.form) {
    control.form.submit();
  }
});
