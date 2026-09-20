def register_blueprints(app):
    from routes import overview, properties, bookings, expenses, documents
    app.register_blueprint(overview.bp)
    app.register_blueprint(properties.bp)
    app.register_blueprint(bookings.bp)
    app.register_blueprint(expenses.bp)
    app.register_blueprint(documents.bp)
