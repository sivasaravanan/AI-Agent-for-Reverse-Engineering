# Security findings

- **DBA-0001 [high]** Hard-coded database credential - `app/main.py:11` - static-detected; not verified at runtime
- **DBA-0002 [high]** Hard-coded database credential - `docker-compose.yml:5` - static-detected; not verified at runtime
- **DBA-0003 [high]** Hard-coded database credential - `dotnet/appsettings.json:1` - static-detected; not verified at runtime
- **DBA-0004 [high]** Hard-coded database credential - `java/application.properties:2` - static-detected; not verified at runtime
- **DBA-0005 [high]** Hard-coded database credential - `php/conn.php:1` - static-detected; not verified at runtime
- **DBA-0006 [high]** Hard-coded database credential - `web/db.js:4` - static-detected; not verified at runtime
- **DBA-0007 [medium]** SQL built by string concatenation/interpolation (heuristic) - `app/main.py:13` - static-detected; not verified at runtime
- **DBA-0008 [low]** Transport encryption / certificate validation disabled - `dotnet/appsettings.json:1` - static-detected; not verified at runtime
- **DBA-0009 [info]** Multiple database technologies detected - `None:None` - static-detected; not verified at runtime
- **DBA-0010 [info]** MongoDB: driver used but no connection configuration found - `web/db.js:2` - static-detected; not verified at runtime
