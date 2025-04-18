sap.ui.define(
    [
        "sap/ui/core/mvc/Controller"
    ],
    function(BaseController) {
      "use strict";
  
      return BaseController.extend("chatui.controller.App", {
        onInit: function() {
          sessionStorage.setItem("isDeployedVersion", "true");
        }
      });
    }
  );
  