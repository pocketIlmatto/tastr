// The web UI's entry point: every module loads (declarations only), then each one's wiring runs in this order.

import { init as navInit } from "./nav.js";
import { init as bulkInit } from "./bulk.js";
import { init as libraryInit } from "./library.js";
import { init as labelsInit } from "./labels.js";
import { init as dawInit } from "./daw.js";
import { init as grooveInit } from "./groove.js";
import { init as menuInit } from "./menu.js";
import { init as uploadInit } from "./upload.js";
import { init as exploreInit } from "./explore.js";
import { init as blendInit } from "./blend.js";
import { init as findInit } from "./find.js";
import { init as adminInit } from "./admin.js";
import { init as helpInit } from "./help.js";
import { init as glossaryInit } from "./glossary.js";
import { init as filesInit } from "./files.js";
import { initChartKeys } from "./chart-loop.js";

navInit();
bulkInit();
libraryInit();
labelsInit();
dawInit();
grooveInit();
menuInit();
uploadInit();
exploreInit();
blendInit();
findInit();
adminInit();
helpInit();
glossaryInit();
filesInit();
initChartKeys();
