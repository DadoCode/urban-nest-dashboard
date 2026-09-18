def register_blueprints(app):
    from routes import overview, properties, occupancy, expenses, documents
    app.register_blueprint(overview.bp)
    app.register_blueprint(properties.bp)
    app.register_blueprint(occupancy.bp)
    app.register_blueprint(expenses.bp)
    app.register_blueprint(documents.bp)
