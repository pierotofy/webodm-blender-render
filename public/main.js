(function(){
  var h = React.createElement;
  var t = window._ || function(s){ return s; };
  var PLUGIN = "/api/plugins/blender-render/task/";

  var getCookie = function(name){
    var m = document.cookie.match(new RegExp("(?:^|;\\s*)" + name + "=([^;]*)"));
    return m ? decodeURIComponent(m[1]) : null;
  };

  var getCamera = function(viewer){
    var cam = viewer.scene.getActiveCamera();
    cam.updateMatrixWorld(true);
    var ortho = !!cam.isOrthographicCamera;
    return {
      matrix: Array.prototype.slice.call(cam.matrixWorld.elements),
      fov: ortho ? 60 : 2 * Math.atan(Math.tan(cam.fov * Math.PI / 360) / (cam.zoom || 1)) * 180 / Math.PI,
      ortho: ortho,
      ortho_height: ortho ? (cam.top - cam.bottom) / (cam.zoom || 1) : 1,
      near: cam.near,
      far: cam.far
    };
  };

  var waitForCompletion = function(celeryTaskId, cb){
    var errors = 0;
    var check = function(){
      fetch("/api/workers/check/" + celeryTaskId, {credentials: "same-origin"})
        .then(function(r){ return r.json(); })
        .then(function(res){
          if (res.error) cb(res.error);
          else if (res.ready) cb();
          else setTimeout(check, 2000);
        })
        .catch(function(e){
          if (errors++ < 10) setTimeout(check, 2000);
          else cb(e.message);
        });
    };
    check();
  };

  class BlenderButton extends React.Component{
    constructor(props){
      super(props);
      this.state = {
        info: null,
        open: false,
        running: false,
        error: "",
        engine: "CYCLES",
        width: 1920,
        height: 1080,
        style: "realistic",
        sky: true,
        sun: true,
        effects: true,
        ground: true
      };
      this.handleDocClick = this.handleDocClick.bind(this);
    }

    componentDidMount(){
      var self = this;
      fetch(PLUGIN + this.props.taskId + "/info", {credentials: "same-origin"})
        .then(function(r){ return r.json(); })
        .then(function(info){ if (!self.unmounted) self.setState({info: info}); })
        .catch(function(){});
      document.addEventListener("mousedown", this.handleDocClick);
    }

    componentWillUnmount(){
      this.unmounted = true;
      document.removeEventListener("mousedown", this.handleDocClick);
    }

    handleDocClick(e){
      if (this.state.open && this.el && !this.el.contains(e.target)) this.setState({open: false});
    }

    run(){
      var self = this;
      var render = this.state.info.blender;
      this.setState({running: true, error: ""});

      var done = function(err){
        if (self.unmounted) return;
        self.setState({running: false, error: err ? String(err) : ""});
      };

      fetch(PLUGIN + this.props.taskId + (render ? "/render" : "/export"), {
        method: "POST",
        credentials: "same-origin",
        headers: {"Content-Type": "application/json", "X-CSRFToken": getCookie("csrftoken") || ""},
        body: JSON.stringify({
          engine: this.state.engine,
          width: parseInt(this.state.width),
          height: parseInt(this.state.height),
          style: this.state.style,
          sky: this.state.sky,
          sun: this.state.sun && this.state.info.sun,
          effects: this.state.effects,
          ground: this.state.ground,
          camera: getCamera(this.props.viewer)
        })
      }).then(function(r){ return r.json(); })
        .then(function(res){
          if (!res.celery_task_id) return done(res.error || res.detail || t("Cannot start task"));
          waitForCompletion(res.celery_task_id, function(err){
            if (err) return done(err);
            window.location.href = "/api/workers/get/" + res.celery_task_id + "?filename=" + encodeURIComponent(res.filename);
            done();
          });
        })
        .catch(function(e){ done(e.message); });
    }

    render(){
      var self = this;
      var info = this.state.info;
      if (!info || !info.glb) return null;

      var label = info.blender ? t("Render") : t("Export to Blender");
      var icon = info.blender ? "fa fa-image" : "fa fa-cube";
      var s = this.state;

      var field = function(text, input){
        return h("div", {className: "form-group", style: {marginBottom: 8}},
          h("label", {style: {fontWeight: "normal", marginBottom: 2}}, text), input);
      };

      var check = function(key, text, disabled){
        return h("div", {className: "checkbox", style: {margin: "2px 0"}},
          h("label", null,
            h("input", {
              type: "checkbox",
              checked: s[key] && !disabled,
              disabled: s.running || disabled,
              onChange: function(e){ var u = {}; u[key] = e.target.checked; self.setState(u); }
            }), " ", text));
      };

      return h("div", {className: "btn-group dropup", ref: function(el){ self.el = el; }},
        h("button", {
          type: "button",
          className: "btn btn-sm btn-secondary",
          title: label,
          onClick: function(){ self.setState({open: !s.open}); }
        }, h("i", {className: s.running ? "fa fa-circle-notch fa-spin" : icon})),
        s.open ? h("div", {className: "dropdown-menu dropdown-menu-right", style: {display: "block", padding: 10, minWidth: 260}},
          field(t("Engine"), h("select", {
            className: "form-control input-sm",
            value: s.engine,
            disabled: s.running,
            onChange: function(e){ self.setState({engine: e.target.value}); }
          }, h("option", {value: "CYCLES"}, "Cycles"), h("option", {value: "EEVEE"}, "EEVEE"))),
          field(t("Resolution"), h("div", {style: {display: "flex", alignItems: "center"}},
            h("input", {type: "number", min: 16, max: 16384, className: "form-control input-sm", value: s.width, disabled: s.running,
              onChange: function(e){ self.setState({width: e.target.value}); }}),
            h("span", {style: {padding: "0 6px"}}, "x"),
            h("input", {type: "number", min: 16, max: 16384, className: "form-control input-sm", value: s.height, disabled: s.running,
              onChange: function(e){ self.setState({height: e.target.value}); }}))),
          field(t("Style"), h("select", {
            className: "form-control input-sm",
            value: s.style,
            disabled: s.running,
            onChange: function(e){ self.setState({style: e.target.value}); }
          }, h("option", {value: "photo"}, t("Photo-accurate")), h("option", {value: "realistic"}, t("Realistic")))),
          s.style === "realistic" ? h("div", {style: {marginBottom: 8}},
            check("sky", t("Sky and haze"), false),
            check("sun", info.sun ? t("Sun matched to photos") : t("Sun (no capture time/GPS)"), !info.sun),
            check("effects", t("Depth of field and tone mapping"), false),
            check("ground", t("Ground plane"), false)) : null,
          s.error ? h("div", {className: "text-danger", style: {marginBottom: 8, whiteSpace: "pre-wrap", maxHeight: 200, overflow: "auto", fontSize: 11}}, s.error) : null,
          h("button", {
            type: "button",
            className: "btn btn-sm btn-primary btn-block",
            disabled: s.running,
            onClick: function(){ self.run(); }
          }, s.running ? h("i", {className: "fa fa-circle-notch fa-spin"}) : h("i", {className: icon}), " ", s.running ? t("Processing...") : label)
        ) : null
      );
    }
  }

  PluginsAPI.ModelView.addActionButton(function(options){
    var m = window.location.pathname.match(/\/task\/([^\/]+)\//);
    if (!m) return null;
    return h(BlenderButton, {viewer: options.viewer, taskId: m[1]});
  });
})();
