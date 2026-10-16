// "Load default index tickers" button on the Competition add/change form
// (league/admin.py's CompetitionAdmin, league/templates/admin/league/
// competition/change_form.html). Fills the index_tickers JSONField's
// textarea with DEFAULT_INDEX_TICKERS (league/models.py) -- the operator
// still has to review/trim and click Save, this just saves retyping ~250
// tickers by hand.
document.addEventListener("DOMContentLoaded", function () {
    var field = document.getElementById("id_index_tickers");
    var dataEl = document.getElementById("default-index-tickers");
    if (!field || !dataEl) {
        return;
    }
    var defaults = JSON.parse(dataEl.textContent);

    var button = document.createElement("button");
    button.type = "button";
    button.textContent = "Load default index tickers";
    button.className = "button";
    button.style.marginTop = "8px";
    button.addEventListener("click", function () {
        field.value = JSON.stringify(defaults, null, 2);
    });
    field.insertAdjacentElement("afterend", button);
});
