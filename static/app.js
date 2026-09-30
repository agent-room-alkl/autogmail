document.querySelectorAll("form[data-confirm]").forEach(function (form) {
  form.addEventListener("submit", function (event) {
    var message = form.getAttribute("data-confirm");
    if (message && !window.confirm(message)) {
      event.preventDefault();
      return;
    }
    var button = form.querySelector("button[type=submit]");
    if (button) {
      button.disabled = true;
      button.textContent = "正在跑这一轮…";
    }
  });
});
