// ============================================================================
// TRUE 1024x768 (4:3) PIXEL COORDINATE SYSTEM (NO VERTICAL STRETCHING!)
// ============================================================================
const IMG_W = 1024;
const IMG_H = 768;
const SCALE = 0.04; // 1 pixel = 0.04m in 3D space (1024x768 -> 40.96m x 30.72m)
const CENTER_X = 580;
const CENTER_Y = 410;

function toX(px) {
  return (px - CENTER_X) * SCALE;
}

function toZ(py) {
  return (py - CENTER_Y) * SCALE;
}

// Wall & Architectural Heights (in meters)
const H_WALL = 2.75;
const H_DOOR = 2.12;
const H_SILL = 0.85;
const H_WIN_TOP = 2.25;
const FLOOR_THICK = 0.12;

// ============================================================================
// SCENE, CAMERA, RENDERER, CONTROLS
// ============================================================================
const container = document.getElementById('canvas-container');
const scene = new THREE.Scene();
scene.background = new THREE.Color(0x111318);

const camera = new THREE.PerspectiveCamera(40, window.innerWidth / window.innerHeight, 0.08, 250);
camera.position.set(-8, 30, 22);

const renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: 'high-performance' });
renderer.setSize(window.innerWidth, window.innerHeight);
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
renderer.shadowMap.enabled = true;
renderer.shadowMap.type = THREE.PCFSoftShadowMap;
renderer.outputEncoding = THREE.sRGBEncoding;
container.appendChild(renderer.domElement);

const controls = new THREE.OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
controls.dampingFactor = 0.07;
controls.maxPolarAngle = Math.PI / 2 - 0.02;
controls.minDistance = 0.2;
controls.maxDistance = 85;
controls.target.set(toX(690), 0, toZ(420));

// ============================================================================
// LIGHTING & SUBTLE GROUND PLANE
// ============================================================================
const hemiLight = new THREE.HemisphereLight(0xffffff, 0x1e293b, 0.82);
hemiLight.position.set(0, 50, 0);
scene.add(hemiLight);

const dirLight = new THREE.DirectionalLight(0xfffbeb, 0.72);
dirLight.position.set(18, 38, 22);
dirLight.castShadow = true;
dirLight.shadow.mapSize.width = 2048;
dirLight.shadow.mapSize.height = 2048;
dirLight.shadow.camera.near = 5;
dirLight.shadow.camera.far = 90;
const d = 26;
dirLight.shadow.camera.left = -d;
dirLight.shadow.camera.right = d;
dirLight.shadow.camera.top = d;
dirLight.shadow.camera.bottom = -d;
dirLight.shadow.bias = -0.0005;
scene.add(dirLight);

const fillLight = new THREE.DirectionalLight(0x93c5fd, 0.25);
fillLight.position.set(-25, 20, -18);
scene.add(fillLight);

// Subtle dark architectural ground grid below everything
const gridHelper = new THREE.GridHelper(64, 64, 0x262b38, 0x191d26);
gridHelper.position.y = -FLOOR_THICK - 0.06;
scene.add(gridHelper);

const wallRecordsForBlueprint = [];

// Shadow-receiving base plane slightly below
const groundGeo = new THREE.PlaneGeometry(120, 120);
const groundMat = new THREE.ShadowMaterial({ opacity: 0.28 });
const groundMesh = new THREE.Mesh(groundGeo, groundMat);
groundMesh.rotation.x = -Math.PI / 2;
groundMesh.position.y = -FLOOR_THICK - 0.02;
groundMesh.receiveShadow = true;
scene.add(groundMesh);

// ============================================================================
// APARTMENT GROUPS & MATERIALS
// ============================================================================
const groups = {
  w1: {
    root: new THREE.Group(),
    walls: new THREE.Group(),
    floors: new THREE.Group(),
    details: new THREE.Group()
  },
  w2: {
    root: new THREE.Group(),
    walls: new THREE.Group(),
    floors: new THREE.Group(),
    details: new THREE.Group()
  },
  common: {
    root: new THREE.Group(),
    walls: new THREE.Group(),
    floors: new THREE.Group(),
    details: new THREE.Group()
  }
};

Object.values(groups).forEach(g => {
  g.root.add(g.floors);
  g.root.add(g.walls);
  g.root.add(g.details);
  scene.add(g.root);
});

function createSectionMaterials(palette) {
  return {
    wall: new THREE.MeshStandardMaterial({
      color: palette.wallColor,
      roughness: 0.55,
      metalness: 0.05,
      transparent: true,
      opacity: 1.0
    }),
    wallCap: new THREE.MeshStandardMaterial({
      color: palette.wallCapColor,
      roughness: 0.4,
      metalness: 0.05,
      transparent: true,
      opacity: 1.0
    }),
    floor: new THREE.MeshStandardMaterial({
      color: palette.floorColor,
      roughness: 0.65,
      metalness: 0.05,
      transparent: true,
      opacity: 0.96
    }),
    floorSub: new THREE.MeshStandardMaterial({
      color: palette.floorSubColor,
      roughness: 0.7,
      metalness: 0.05,
      transparent: true,
      opacity: 0.96
    }),
    doorLeaf: new THREE.MeshStandardMaterial({
      color: palette.doorColor,
      roughness: 0.5,
      metalness: 0.1,
      transparent: true,
      opacity: 1.0
    }),
    railingMetal: new THREE.MeshStandardMaterial({
      color: 0x475569,
      roughness: 0.35,
      metalness: 0.45,
      transparent: true,
      opacity: 1.0
    }),
    glass: new THREE.MeshPhysicalMaterial({
      color: 0x7dd3fc,
      roughness: 0.15,
      metalness: 0.1,
      transparent: true,
      opacity: 0.36
    }),
    durchgangFloor: new THREE.MeshStandardMaterial({
      color: 0xfde047,
      roughness: 0.6,
      metalness: 0.05,
      transparent: true,
      opacity: 0.85
    }),
    badNeuFloor: new THREE.MeshStandardMaterial({
      color: 0xfb923c,
      roughness: 0.6,
      metalness: 0.05,
      transparent: true,
      opacity: 0.80
    }),
    arcLine: new THREE.LineBasicMaterial({
      color: palette.arcColor,
      transparent: true,
      opacity: 0.85
    })
  };
}

const mats = {
  w1: createSectionMaterials({
    wallColor: 0xf1f5f9,
    wallCapColor: 0x38bdf8,
    floorColor: 0xdbeafe,
    floorSubColor: 0xbae6fd,
    doorColor: 0x7dd3fc,
    arcColor: 0x0284c7
  }),
  w2: createSectionMaterials({
    wallColor: 0xf1f5f9,
    wallCapColor: 0x34d399,
    floorColor: 0xd1fae5,
    floorSubColor: 0xa7f3d0,
    doorColor: 0x6ee7b7,
    arcColor: 0x059669
  }),
  common: createSectionMaterials({
    wallColor: 0xd1d5db,
    wallCapColor: 0x9ca3af,
    floorColor: 0xe5e7eb,
    floorSubColor: 0xd1d5db,
    doorColor: 0x9ca3af,
    arcColor: 0x6b7280
  })
};

// ============================================================================
// GEOMETRY BUILDERS (FLOORS, WALLS, DOORS, WINDOWS, ARCS)
// ============================================================================
function addRectFloor(sectionKey, x1, y1, x2, y2, isSub = false, customHeight = FLOOR_THICK, yOffset = 0, matKey = null) {
  const w = Math.abs(x2 - x1) * SCALE;
  const d = Math.abs(y2 - y1) * SCALE;
  const cx = toX((x1 + x2) / 2);
  const cz = toZ((y1 + y2) / 2);

  const geo = new THREE.BoxGeometry(w, customHeight, d);
  const mat = matKey ? mats[sectionKey][matKey] : (isSub ? mats[sectionKey].floorSub : mats[sectionKey].floor);
  const mesh = new THREE.Mesh(geo, mat);
  mesh.position.set(cx, -customHeight / 2 + yOffset, cz);
  mesh.receiveShadow = true;
  groups[sectionKey].floors.add(mesh);

  const edges = new THREE.EdgesGeometry(geo);
  const line = new THREE.LineSegments(edges, mats[sectionKey].arcLine);
  line.position.copy(mesh.position);
  groups[sectionKey].details.add(line);
}

function addPolyFloor(sectionKey, points, isSub = false, customHeight = FLOOR_THICK, yOffset = 0, matKey = null) {
  const shape = new THREE.Shape();
  points.forEach(([px, py], idx) => {
    const sx = toX(px);
    const sy = -toZ(py);
    if (idx === 0) shape.moveTo(sx, sy);
    else shape.lineTo(sx, sy);
  });
  shape.closePath();

  const geo = new THREE.ExtrudeGeometry(shape, { depth: customHeight, bevelEnabled: false });
  const mat = matKey ? mats[sectionKey][matKey] : (isSub ? mats[sectionKey].floorSub : mats[sectionKey].floor);
  const mesh = new THREE.Mesh(geo, mat);
  mesh.rotation.x = -Math.PI / 2;
  mesh.position.y = -customHeight + yOffset;
  mesh.receiveShadow = true;
  groups[sectionKey].floors.add(mesh);

  const edges = new THREE.EdgesGeometry(geo);
  const line = new THREE.LineSegments(edges, mats[sectionKey].arcLine);
  line.rotation.x = -Math.PI / 2;
  line.position.y = -customHeight + yOffset;
  groups[sectionKey].details.add(line);
}

function parseEndExt(extVal, halfThickPx) {
  if (Array.isArray(extVal)) return [extVal[0] * SCALE, extVal[1] * SCALE];
  if (typeof extVal === 'number') return [extVal * SCALE, extVal * SCALE];
  if (extVal === true) return [halfThickPx * SCALE, halfThickPx * SCALE];
  return [0, 0];
}

function createTrapezoidPrismGeo(x0Minus, x0Plus, x1Minus, x1Plus, halfZ, height) {
  const shape = new THREE.Shape();
  shape.moveTo(x0Minus, halfZ);
  shape.lineTo(x1Minus, halfZ);
  shape.lineTo(x1Plus, -halfZ);
  shape.lineTo(x0Plus, -halfZ);
  shape.closePath();
  return new THREE.ExtrudeGeometry(shape, { depth: height, bevelEnabled: false });
}

function createWallBoxSegment(sectionKey, wallGroup, tStart, tEnd, yBottom, yTop, totalLen, thick3d, e1Pair, e2Pair) {
  const segLen = (tEnd - tStart) * totalLen;
  if (segLen <= 0.005 || yTop - yBottom <= 0.005) return;

  const segHeight = yTop - yBottom;
  const halfZ = thick3d / 2;
  const wInfo = wallGroup.userData || { x1: 0, y1: 0, x2: 0, y2: 0 };
  const numSteps = Math.max(1, Math.ceil(segLen / (18 * SCALE)));

  for (let i = 0; i < numSteps; i++) {
    const subT1 = tStart + (tEnd - tStart) * (i / numSteps);
    const subT2 = tStart + (tEnd - tStart) * ((i + 1) / numSteps);
    const tMid = (subT1 + subT2) / 2;
    const midPx = wInfo.x1 + (wInfo.x2 - wInfo.x1) * tMid;
    const midPy = wInfo.y1 + (wInfo.y2 - wInfo.y1) * tMid;

    const x0Minus = (subT1 <= 0.001) ? (-totalLen / 2 - e1Pair[0]) : ((subT1 - 0.5) * totalLen);
    const x0Plus  = (subT1 <= 0.001) ? (-totalLen / 2 - e1Pair[1]) : ((subT1 - 0.5) * totalLen);
    const x1Minus = (subT2 >= 0.999) ? ( totalLen / 2 + e2Pair[0]) : ((subT2 - 0.5) * totalLen);
    const x1Plus  = (subT2 >= 0.999) ? ( totalLen / 2 + e2Pair[1]) : ((subT2 - 0.5) * totalLen);

    const geo = createTrapezoidPrismGeo(x0Minus, x0Plus, x1Minus, x1Plus, halfZ, segHeight);
    const mesh = new THREE.Mesh(geo, mats[sectionKey].wall);
    mesh.rotation.x = -Math.PI / 2;
    mesh.position.set(0, yBottom, 0);
    mesh.castShadow = true;
    mesh.receiveShadow = true;
    mesh.userData = {
      isWallPiece: true,
      isCap: false,
      yBottom,
      origMat: mats[sectionKey].wall,
      sectionKey,
      midPx,
      midPy,
      dx: wInfo.x2 - wInfo.x1,
      dy: wInfo.y2 - wInfo.y1
    };
    wallGroup.add(mesh);

    if (Math.abs(yTop - H_WALL) < 0.02) {
      const capGeo = createTrapezoidPrismGeo(x0Minus, x0Plus, x1Minus, x1Plus, halfZ, 0.04);
      const capMesh = new THREE.Mesh(capGeo, mats[sectionKey].wallCap);
      capMesh.rotation.x = -Math.PI / 2;
      capMesh.position.set(0, H_WALL, 0);
      capMesh.userData = {
        isWallPiece: true,
        isCap: true,
        origMat: mats[sectionKey].wallCap,
        sectionKey,
        midPx,
        midPy,
        dx: wInfo.x2 - wInfo.x1,
        dy: wInfo.y2 - wInfo.y1
      };
      wallGroup.add(capMesh);
    }
  }
}

function addWall(sectionKey, x1, y1, x2, y2, thick = 12, openings = [], customWallHeight = H_WALL, extEnds = null) {
  const L0 = Math.hypot(x2 - x1, y2 - y1);
  if (L0 < 0.5) return;
  const axisAligned = (x1 === x2 || y1 === y2);
  const ends = extEnds || (axisAligned ? [true, true] : [false, false]);
  const e1Pair = parseEndExt(ends[0], thick / 2);
  const e2Pair = parseEndExt(ends[1], thick / 2);

  const p1x = toX(x1);
  const p1z = toZ(y1);
  const p2x = toX(x2);
  const p2z = toZ(y2);

  const dx = p2x - p1x;
  const dz = p2z - p1z;
  const totalLen = Math.hypot(dx, dz);
  if (totalLen < 0.01) return;

  const angle = Math.atan2(-dz, dx);
  const thick3d = thick * SCALE;

  const wallGroup = new THREE.Group();
  wallGroup.position.set((p1x + p2x) / 2, 0, (p1z + p2z) / 2);
  wallGroup.rotation.y = angle;
  wallGroup.userData = { x1, y1, x2, y2, sectionKey };
  groups[sectionKey].walls.add(wallGroup);

  const isHorizontal = Math.abs(x2 - x1) >= Math.abs(y2 - y1);
  const startCoord = isHorizontal ? x1 : y1;
  const endCoord = isHorizontal ? x2 : y2;
  const spanCoord = endCoord - startCoord;

  const normOpenings = openings.map(op => {
    let t1, t2;
    if (op.t1 !== undefined && op.t2 !== undefined) {
      t1 = op.t1;
      t2 = op.t2;
    } else {
      t1 = (op.from - startCoord) / spanCoord;
      t2 = (op.to - startCoord) / spanCoord;
    }
    if (t1 > t2) [t1, t2] = [t2, t1];
    t1 = Math.max(0.01, Math.min(0.99, t1));
    t2 = Math.max(0.01, Math.min(0.99, t2));
    return { ...op, t1, t2 };
  }).sort((a, b) => a.t1 - b.t1);

  // Save record for synthetic blueprint canvas
  wallRecordsForBlueprint.push({ x1, y1, x2, y2, thick, openings: normOpenings });

  let cursor = 0;
  normOpenings.forEach(op => {
    if (op.t1 > cursor) {
      createWallBoxSegment(sectionKey, wallGroup, cursor, op.t1, 0, customWallHeight, totalLen, thick3d, e1Pair, e2Pair);
    }

    const opLen = (op.t2 - op.t1) * totalLen;
    const opMidT = (op.t1 + op.t2) / 2;
    const centerLocalX = (opMidT - 0.5) * totalLen;
    const opMidPx = x1 + (x2 - x1) * opMidT;
    const opMidPy = y1 + (y2 - y1) * opMidT;
    const makeOpUserData = (origMat) => ({
      isWallPiece: true,
      isOpening: true,
      isCap: false,
      origMat,
      shutterRoomId: op.roomId || null,
      sectionKey,
      midPx: opMidPx,
      midPy: opMidPy,
      dx: x2 - x1,
      dy: y2 - y1
    });

    if (op.type === 'door' || op.type === 'frame') {
      if (customWallHeight > H_DOOR) {
        createWallBoxSegment(sectionKey, wallGroup, op.t1, op.t2, H_DOOR, customWallHeight, totalLen, thick3d, e1Pair, e2Pair);
      }
      if (op.shutterId) {
        const doorH = Math.max(0.2, Math.min(customWallHeight, H_DOOR));
        const glassGeo = new THREE.BoxGeometry(opLen, doorH, Math.max(0.04, thick3d * 0.28));
        const glassMesh = new THREE.Mesh(glassGeo, mats[sectionKey].glass);
        glassMesh.position.set(centerLocalX, doorH / 2, 0);
        glassMesh.userData = makeOpUserData(mats[sectionKey].glass);
        wallGroup.add(glassMesh);
      }
    } else if (op.type === 'passage') {
      // Durchgang: komplett frei
    } else if (op.type === 'window') {
      const sill = op.lowSill ? 0.25 : H_SILL;
      createWallBoxSegment(sectionKey, wallGroup, op.t1, op.t2, 0, sill, totalLen, thick3d, e1Pair, e2Pair);
      if (customWallHeight > H_WIN_TOP) {
        createWallBoxSegment(sectionKey, wallGroup, op.t1, op.t2, H_WIN_TOP, customWallHeight, totalLen, thick3d, e1Pair, e2Pair);
      }
      const winHeight = Math.max(0.2, Math.min(customWallHeight, H_WIN_TOP) - sill);
      const glassGeo = new THREE.BoxGeometry(opLen, winHeight, Math.max(0.04, thick3d * 0.28));
      const glassMesh = new THREE.Mesh(glassGeo, mats[sectionKey].glass);
      glassMesh.position.set(centerLocalX, sill + winHeight / 2, 0);
      glassMesh.userData = makeOpUserData(mats[sectionKey].glass);
      wallGroup.add(glassMesh);
    }

    // Automatischer Rollladen (für Fenster UND Balkontüren unterstützt)
    if (op.shutterId && (op.type === 'window' || op.type === 'door')) {
      const isDoorShutter = op.type === 'door';
      const sill = isDoorShutter ? 0 : (op.lowSill ? 0.25 : H_SILL);
      const topLimit = isDoorShutter ? H_DOOR : H_WIN_TOP;
      const winTop = Math.min(customWallHeight, topLimit);
      const winHeight = Math.max(0.2, winTop - sill);

      if (!window.shuttersRegistry) window.shuttersRegistry = [];
      if (!window.shutterSlatMat) {
        const sCanvas = document.createElement('canvas');
        sCanvas.width = 64;
        sCanvas.height = 128;
        const sCtx = sCanvas.getContext('2d');
        for (let sy = 0; sy < 128; sy += 8) {
          sCtx.fillStyle = '#cbd5e1';
          sCtx.fillRect(0, sy, 64, 5);
          sCtx.fillStyle = '#94a3b8';
          sCtx.fillRect(0, sy + 5, 64, 1.5);
          sCtx.fillStyle = '#1e293b';
          sCtx.fillRect(0, sy + 6.5, 64, 1.5);
        }
        const sTex = new THREE.CanvasTexture(sCanvas);
        sTex.wrapS = THREE.RepeatWrapping;
        sTex.wrapT = THREE.RepeatWrapping;
        sTex.repeat.set(1, 3);
        window.shutterSlatMat = new THREE.MeshStandardMaterial({
          map: sTex,
          roughness: 0.45,
          metalness: 0.25
        });
      }

      // 1) Rollladenkasten oben am Sturz
      const boxGeo = new THREE.BoxGeometry(opLen + 0.06, 0.14, thick3d * 0.68);
      const boxMat = new THREE.MeshStandardMaterial({ color: 0x1e293b, roughness: 0.5 });
      const boxMesh = new THREE.Mesh(boxGeo, boxMat);
      boxMesh.position.set(centerLocalX, winTop - 0.02, 0);
      boxMesh.userData = makeOpUserData(boxMat);
      wallGroup.add(boxMesh);

      // 2) Seitliche Führungsschienen links & rechts im Rahmen
      const railGeo = new THREE.BoxGeometry(0.035, winHeight, thick3d * 0.50);
      const railMat = new THREE.MeshStandardMaterial({ color: 0x334155, roughness: 0.5 });
      [-opLen / 2 + 0.018, opLen / 2 - 0.018].forEach(rx => {
        const rMesh = new THREE.Mesh(railGeo, railMat);
        rMesh.position.set(centerLocalX + rx, sill + winHeight / 2, 0);
        rMesh.userData = makeOpUserData(railMat);
        wallGroup.add(rMesh);
      });

      // 3) Beweglicher Rollladenpanzer (Lamellen) + Abschlussleiste unten
      const curtainGeo = new THREE.BoxGeometry(opLen - 0.04, 1.0, thick3d * 0.44);
      const curtainMesh = new THREE.Mesh(curtainGeo, window.shutterSlatMat);
      curtainMesh.castShadow = true;
      curtainMesh.receiveShadow = true;
      curtainMesh.userData = makeOpUserData(window.shutterSlatMat);
      wallGroup.add(curtainMesh);

      const bottomBarGeo = new THREE.BoxGeometry(opLen - 0.03, 0.04, thick3d * 0.48);
      const bottomBarMat = new THREE.MeshStandardMaterial({ color: 0xf59e0b, roughness: 0.4 });
      const bottomBarMesh = new THREE.Mesh(bottomBarGeo, bottomBarMat);
      bottomBarMesh.userData = makeOpUserData(bottomBarMat);
      wallGroup.add(bottomBarMesh);

      // Welt-Koordinate für den schwebenden Rollladen-Button im Raum
      const midT = (op.t1 + op.t2) / 2;
      const wx = p1x + dx * midT;
      const wz = p1z + dz * midT;
      const initPct = op.initialPercent !== undefined ? op.initialPercent : 25;
      const badgeY = isDoorShutter ? 1.15 : (sill + winHeight * 0.68);

      window.shuttersRegistry.push({
        id: op.shutterId,
        name: op.shutterName || 'Rollladen',
        shortLabel: op.shutterLabel || 'Rollladen',
        roomId: op.roomId || null,
        apt: sectionKey,
        currentPercent: initPct,
        targetPercent: initPct,
        sill,
        winTop,
        winHeight,
        centerLocalX,
        curtainMesh,
        bottomBarMesh,
        worldPos: new THREE.Vector3(wx, badgeY, wz)
      });
    }

    cursor = op.t2;
  });

  if (cursor < 1.0) {
    createWallBoxSegment(sectionKey, wallGroup, cursor, 1.0, 0, customWallHeight, totalLen, thick3d, e1Pair, e2Pair);
  }
}

function addBalconyRailing(sectionKey, x1, y1, x2, y2, thick = 5, railHeight = 1.02, extEnds = null) {
  const p1x = toX(x1);
  const p1z = toZ(y1);
  const p2x = toX(x2);
  const p2z = toZ(y2);

  const dx = p2x - p1x;
  const dz = p2z - p1z;
  const totalLen = Math.hypot(dx, dz);
  if (totalLen < 0.01) return;

  const ends = extEnds || [false, false];
  const e1Pair = parseEndExt(ends[0], thick / 2);
  const e2Pair = parseEndExt(ends[1], thick / 2);

  const angle = Math.atan2(-dz, dx);
  const thick3d = thick * SCALE;
  const halfZ = thick3d / 2;

  const x0Minus = -totalLen / 2 - e1Pair[0];
  const x0Plus  = -totalLen / 2 - e1Pair[1];
  const x1Minus =  totalLen / 2 + e2Pair[0];
  const x1Plus  =  totalLen / 2 + e2Pair[1];

  const railGroup = new THREE.Group();
  railGroup.position.set((p1x + p2x) / 2, 0, (p1z + p2z) / 2);
  railGroup.rotation.y = angle;
  groups[sectionKey].walls.add(railGroup);

  const makeRailUserData = (origMat) => ({
    isWallPiece: true,
    isRailing: true,
    isCap: false,
    origMat,
    sectionKey,
    midPx: (x1 + x2) / 2,
    midPy: (y1 + y2) / 2,
    dx: x2 - x1,
    dy: y2 - y1
  });

  const curbH = 0.22;
  const curbGeo = createTrapezoidPrismGeo(x0Minus, x0Plus, x1Minus, x1Plus, halfZ, curbH);
  const curbMesh = new THREE.Mesh(curbGeo, mats[sectionKey].wall);
  curbMesh.rotation.x = -Math.PI / 2;
  curbMesh.position.set(0, 0, 0);
  curbMesh.castShadow = true;
  curbMesh.receiveShadow = true;
  curbMesh.userData = makeRailUserData(mats[sectionKey].wall);
  railGroup.add(curbMesh);

  const glassH = Math.max(0.1, railHeight - curbH - 0.055);
  const glassHalfZ = Math.max(0.018, halfZ * 0.35);
  const glassGeo = createTrapezoidPrismGeo(x0Minus, x0Plus, x1Minus, x1Plus, glassHalfZ, glassH);
  const glassMesh = new THREE.Mesh(glassGeo, mats[sectionKey].glass);
  glassMesh.rotation.x = -Math.PI / 2;
  glassMesh.position.set(0, curbH, 0);
  glassMesh.userData = makeRailUserData(mats[sectionKey].glass);
  railGroup.add(glassMesh);

  const topBarGeo = createTrapezoidPrismGeo(x0Minus, x0Plus, x1Minus, x1Plus, halfZ + 0.008, 0.055);
  const topBarMesh = new THREE.Mesh(topBarGeo, mats[sectionKey].railingMetal);
  topBarMesh.rotation.x = -Math.PI / 2;
  topBarMesh.position.set(0, railHeight - 0.055, 0);
  topBarMesh.castShadow = true;
  topBarMesh.userData = makeRailUserData(mats[sectionKey].railingMetal);
  railGroup.add(topBarMesh);
}

// ============================================================================
// BUILD PIXEL-CALIBRATED 1024x768 GEOMETRY
// ============================================================================

// 1. WOHNUNG 1 — OBEN / OST (px: 638..951, py: 110..445)
(function buildWohnung1() {
  // Big North Room (Zimmer 1: px = 638..812, py = 110..330)
  addRectFloor('w1', 638, 110, 812, 330);
  // North-East Balcony (Balkon: straight right edge at px = 893 aligned with wall below)
  addRectFloor('w1', 812, 110, 893, 221, true);
  // Middle-East Room below balcony (Zimmer 2: px = 812..893, py = 221..330)
  addRectFloor('w1', 812, 221, 893, 330);
  // Central Hallway & Internal Bath/WC (px = 638..760, py = 292..445)
  addRectFloor('w1', 638, 330, 760, 445);
  // East Angular Bay Room (Erker-Zimmer: px = 760..950.5, py = 330..445)
  addPolyFloor('w1', [
    [760, 330],
    [893, 330],
    [950.5, 387.5],
    [893, 445],
    [760, 445]
  ]);

  // Durchgehende, glatte Westwand von Wohnung 1 (px = 638, py: 110..400, Stärke 16; ab 400..445 offener Durchgang)
  addWall('w1', 638, 110, 638, 400, 16, [], H_WALL, [8, 0]);
  // Nordwand von Simons Zimmer (py = 110, px: 638..812, schließt rechts bei 812+5=817 exakt bündig mit der 10er-Wand ab!)
  addWall('w1', 638, 110, 812, 110, 16, [], H_WALL, [8, 5]);

  // Durchgehende, komplett glatte Wand bei px = 812 (py: 110..330, Stärke 10, oben um 8 px verlängert für 100% bündige Ecke!)
  addWall('w1', 812, 110, 812, 330, 10, [
    {
      type: 'window',
      from: 132,
      to: 185,
      shutterId: 'shutter-simon-win',
      shutterName: 'Simons Zimmer (Fenster)',
      shutterLabel: 'Rollladen Fenster',
      roomId: 'label-simon',
      initialPercent: 25
    },
    {
      type: 'door',
      from: 186,
      to: 216,
      swingDir: -1,
      hinge: 'end',
      shutterId: 'shutter-simon-door',
      shutterName: 'Simons Zimmer (Balkontür)',
      shutterLabel: 'Rollladen Tür',
      roomId: 'label-simon',
      initialPercent: 25
    },
    { type: 'door', from: 294, to: 324, swingDir: 1, hinge: 'end' }
  ], H_WALL, [8, 5]);

  // Wand zwischen Arbeitszimmer und Balkon (py = 221, px: 812..893, Stärke 10, schließt rechts bei 893+8=901 exakt bündig ab!)
  addWall('w1', 812, 221, 893, 221, 10, [
    {
      type: 'window',
      from: 826,
      to: 874,
      shutterId: 'shutter-arbeit',
      shutterName: 'Arbeitszimmer',
      shutterLabel: 'Rollladen',
      roomId: 'label-arbeit',
      initialPercent: 25
    }
  ], H_WALL, [-5, 8]);

  // Outer East Wall of Middle-East Room (px = 893, py: 221..330) – oben exakt bündig (+5), unten echter Gehrungsschnitt zum Erker!
  const m22_5 = 8 * (Math.SQRT2 - 1); // 3.3137 px für 45°-Knick bei Wandstärke 16
  addWall('w1', 893, 221, 893, 330, 16, [], H_WALL, [5, [-m22_5, m22_5]]);

  // North-East Balcony Railing mit exakter Gehrungsecke bei (893, 110) und bündigem Wandanschluss
  addBalconyRailing('w1', 817, 110, 893, 110, 5, 1.02, [2, [2.5, -2.5]]);
  addBalconyRailing('w1', 893, 110, 893, 216, 5, 1.02, [[2.5, -2.5], 2]);

  // Durchgehende, glatte Südwand von Simons Zimmer (py = 248, px: 638..812, Stärke 10)
  addWall('w1', 638, 248, 812, 248, 10, [
    { type: 'door', from: 754, to: 786, swingDir: 1, hinge: 'start' }
  ], H_WALL, [-8, -5]);

  // Durchgehende, glatte Ostwand der Abstellkammer + Flur-Durchgang (px = 712, py: 248..330, Stärke 10)
  addWall('w1', 712, 248, 712, 330, 10, [
    { type: 'door', from: 297, to: 325, swingDir: 1, hinge: 'end' }
  ], H_WALL, [-5, 5]);

  // Südwand der Abstellkammer (py = 292, px: 638..712, Stärke 10)
  addWall('w1', 638, 292, 712, 292, 10, [
    { type: 'door', from: 662, to: 696, swingDir: 1, hinge: 'start' }
  ], H_WALL, [-8, -5]);

  // Durchgehende Horizontalwand bei py = 330 (px: 712..893, Stärke 10 – innen auf Gehrung mit dem Erker verbunden!)
  addWall('w1', 712, 330, 893, 330, 10, [], H_WALL, [5, [-5, -(3 + m22_5)]]);

  // Neues Badezimmer (L-Form): px 684..760, py 362..445 – alle Wände einheitlich Stärke 10 und glatt bündig
  addPolyFloor('w1', [
    [684, 362],
    [714, 362],
    [714, 386],
    [760, 386],
    [760, 445],
    [684, 445]
  ], false, FLOOR_THICK, 0.012, 'badNeuFloor');
  addWall('w1', 684, 362, 684, 445, 10, [], H_WALL, [5, -8]);
  addWall('w1', 684, 362, 714, 362, 10, [], H_WALL, [5, 5]);
  addWall('w1', 714, 362, 714, 386, 10, [], H_WALL, [5, 5]);
  addWall('w1', 714, 386, 760, 386, 10, [
    { type: 'door', from: 722, to: 752, swingDir: 1, hinge: 'end' }
  ], H_WALL, [5, -5]);

  // Wall separating Inner Hallway from Bay-Window Room (px = 760, py: 330..445, Stärke 10)
  addWall('w1', 760, 330, 760, 445, 10, [
    { type: 'door', from: 342, to: 374, swingDir: 1, hinge: 'start' }
  ], H_WALL, [-5, -8]);

  // East Angled Bay Window Walls mit geschlossenen Gehrungsschnitten bei (893, 330), (950.5, 387.5) und (893, 445)!
  addWall('w1', 893, 330, 950.5, 387.5, 16, [
    {
      type: 'window',
      t1: 0.18,
      t2: 0.82,
      shutterId: 'shutter-martin',
      shutterName: 'Martins Zimmer',
      shutterLabel: 'Rollladen',
      roomId: 'label-martin',
      initialPercent: 25
    }
  ], H_WALL, [[-m22_5, m22_5], [8, -8]]);
  addWall('w1', 950.5, 387.5, 893, 445, 16, [], H_WALL, [[8, -8], [-m22_5, m22_5]]);

  // Dividing Wall between Wohnung 1 and Wohnung 2 (py = 445, px: 638..893 – schließt das Dreieck innen komplett ohne Überstand!)
  addWall('w1', 638, 445, 893, 445, 16, [], H_WALL, [8, [-m22_5, 0]]);
})();

// ============================================================================
// TV & MONITOR DISPLAY SYSTEM (OPTISCHE AN / AUS ANZEIGE AUF ALLEN GERÄTEN)
// ============================================================================
window.screensRegistry = [];

function createScreenCanvasTex(kind, state) {
  const c = document.createElement('canvas');
  c.width = 256;
  c.height = 144;
  const ctx = c.getContext('2d');

  if (!state) {
    // AUS: Tiefschwarzes mattes Display mit leichtem dunklen Reflex & Standby-Anzeige
    const offGrad = ctx.createLinearGradient(0, 0, 256, 144);
    offGrad.addColorStop(0, '#070a12');
    offGrad.addColorStop(0.5, '#0f172a');
    offGrad.addColorStop(1, '#05080f');
    ctx.fillStyle = offGrad;
    ctx.fillRect(0, 0, 256, 144);
    // Dezenter dunkler Standby-Schriftzug in der Mitte
    ctx.fillStyle = 'rgba(239, 68, 68, 0.72)';
    ctx.beginPath();
    ctx.arc(128, 64, 7, 0, Math.PI * 2);
    ctx.fill();
    ctx.fillStyle = 'rgba(148, 163, 184, 0.55)';
    ctx.font = 'bold 14px Inter, sans-serif';
    ctx.textAlign = 'center';
    ctx.fillText('AUS (STANDBY)', 128, 94);
  } else if (kind === 'tv') {
    // AN (TV): Leuchtendes Smart-TV Heimkino-Bild mit bunter Lichtstimmung & Player-Leiste
    const bg = ctx.createLinearGradient(0, 0, 256, 144);
    bg.addColorStop(0, '#0284c7');
    bg.addColorStop(0.45, '#4f46e5');
    bg.addColorStop(1, '#c026d3');
    ctx.fillStyle = bg;
    ctx.fillRect(0, 0, 256, 144);

    // Leuchtende Sonne / Horizont-Kreis
    const sun = ctx.createRadialGradient(128, 56, 4, 128, 56, 52);
    sun.addColorStop(0, '#fef08a');
    sun.addColorStop(0.5, 'rgba(56, 189, 248, 0.65)');
    sun.addColorStop(1, 'rgba(56, 189, 248, 0)');
    ctx.fillStyle = sun;
    ctx.fillRect(0, 0, 256, 144);

    // Untere Medien-Player-Bar
    ctx.fillStyle = 'rgba(15, 23, 42, 0.72)';
    ctx.fillRect(16, 108, 224, 22);
    ctx.fillStyle = '#38bdf8';
    ctx.fillRect(24, 117, 135, 5);
    ctx.fillStyle = 'rgba(255,255,255,0.28)';
    ctx.fillRect(159, 117, 72, 5);

    // Grünes "LIVE / AN" Badge oben links auf dem Bildschirm
    ctx.fillStyle = 'rgba(6, 78, 59, 0.85)';
    ctx.fillRect(14, 12, 66, 22);
    ctx.fillStyle = '#4ade80';
    ctx.beginPath();
    ctx.arc(26, 23, 4.5, 0, Math.PI * 2);
    ctx.fill();
    ctx.fillStyle = '#ffffff';
    ctx.font = 'bold 12px Inter, sans-serif';
    ctx.textAlign = 'left';
    ctx.fillText('TV AN', 35, 27);
  } else {
    // AN (PC-Monitor): Leuchtender Desktop mit Fenstern & Taskleiste
    const bg = ctx.createLinearGradient(0, 0, 256, 144);
    bg.addColorStop(0, '#0369a1');
    bg.addColorStop(0.55, '#0284c7');
    bg.addColorStop(1, '#0f172a');
    ctx.fillStyle = bg;
    ctx.fillRect(0, 0, 256, 144);

    // Linkes App-/Code-Fenster auf dem Monitor
    ctx.fillStyle = 'rgba(15, 23, 42, 0.84)';
    ctx.fillRect(14, 14, 134, 98);
    ctx.fillStyle = '#38bdf8';
    ctx.fillRect(22, 26, 58, 6);
    ctx.fillStyle = '#a855f7';
    ctx.fillRect(22, 38, 92, 6);
    ctx.fillStyle = '#4ade80';
    ctx.fillRect(22, 50, 76, 6);
    ctx.fillStyle = '#facc15';
    ctx.fillRect(22, 62, 104, 6);
    ctx.fillStyle = '#94a3b8';
    ctx.fillRect(22, 74, 64, 6);

    // Rechtes Widget-Fenster
    ctx.fillStyle = 'rgba(30, 41, 59, 0.86)';
    ctx.fillRect(156, 14, 86, 98);
    ctx.fillStyle = '#22c55e';
    ctx.beginPath();
    ctx.arc(199, 52, 18, 0, Math.PI * 2);
    ctx.fill();
    ctx.fillStyle = '#ffffff';
    ctx.font = 'bold 11px Inter, sans-serif';
    ctx.textAlign = 'center';
    ctx.fillText('AN', 199, 56);

    // Untere Taskleiste
    ctx.fillStyle = 'rgba(9, 13, 22, 0.9)';
    ctx.fillRect(0, 124, 256, 20);
    ctx.fillStyle = '#38bdf8';
    ctx.fillRect(108, 129, 12, 10);
    ctx.fillStyle = '#22c55e';
    ctx.fillRect(126, 129, 12, 10);
    ctx.fillStyle = '#f59e0b';
    ctx.fillRect(144, 129, 12, 10);
  }

  const tex = new THREE.CanvasTexture(c);
  tex.needsUpdate = true;
  return tex;
}

const screenTextures = {
  tvOn: createScreenCanvasTex('tv', true),
  tvOff: createScreenCanvasTex('tv', false),
  monOn: createScreenCanvasTex('monitor', true),
  monOff: createScreenCanvasTex('monitor', false)
};

function registerScreen(cfg) {
  // cfg: { id, name, shortLabel, kind: 'tv'|'monitor', roomId, apt, mesh, x1, y1, x2, y2, h, yOff, faceDir: 'N'|'S'|'E'|'W', isOn }
  const mat = new THREE.MeshStandardMaterial({
    color: 0xffffff,
    roughness: 0.18,
    metalness: 0.1
  });
  cfg.mesh.material = mat;
  cfg.mesh.userData.origMat = mat;

  // Kleine Status-LED (Grün = AN, Rot = Standby/AUS) unten am Rahmen des Geräts
  const ledMat = new THREE.MeshBasicMaterial({ color: cfg.isOn ? 0x22c55e : 0xef4444 });
  const ledDot = new THREE.Mesh(new THREE.SphereGeometry(0.018, 12, 12), ledMat);
  const midX = toX((cfg.x1 + cfg.x2) / 2);
  const midZ = toZ((cfg.y1 + cfg.y2) / 2);
  const bottomY = cfg.yOff + 0.015;
  const offsetOut = 0.018;
  if (cfg.faceDir === 'S') {
    ledDot.position.set(toX(cfg.x2 - 2.2), bottomY, toZ(cfg.y2) + offsetOut);
  } else if (cfg.faceDir === 'N') {
    ledDot.position.set(toX(cfg.x1 + 2.2), bottomY, toZ(cfg.y1) - offsetOut);
  } else if (cfg.faceDir === 'E') {
    ledDot.position.set(toX(cfg.x2) + offsetOut, bottomY, toZ(cfg.y2 - 1.8));
  } else {
    ledDot.position.set(toX(cfg.x1) - offsetOut, bottomY, toZ(cfg.y1 + 1.8));
  }
  groups[cfg.apt].details.add(ledDot);

  // Sanfter 3D-Leuchtschein direkt vor dem Bildschirm (sichtbar wenn AN)
  const spanW = Math.max(Math.abs(cfg.x2 - cfg.x1), Math.abs(cfg.y2 - cfg.y1)) * SCALE;
  const glowMat = new THREE.MeshBasicMaterial({
    color: cfg.kind === 'tv' ? 0x38bdf8 : 0x0ea5e9,
    transparent: true,
    opacity: 0.18,
    side: THREE.DoubleSide,
    depthWrite: false
  });
  const glowMesh = new THREE.Mesh(
    new THREE.PlaneGeometry(spanW * 1.12, cfg.h * 1.15),
    glowMat
  );
  const centerY = cfg.yOff + cfg.h / 2;
  const glowOffset = 0.025;
  if (cfg.faceDir === 'S') {
    glowMesh.position.set(midX, centerY, toZ(cfg.y2) + glowOffset);
  } else if (cfg.faceDir === 'N') {
    glowMesh.position.set(midX, centerY, toZ(cfg.y1) - glowOffset);
  } else if (cfg.faceDir === 'E') {
    glowMesh.rotation.y = Math.PI / 2;
    glowMesh.position.set(toX(cfg.x2) + glowOffset, centerY, midZ);
  } else {
    glowMesh.rotation.y = Math.PI / 2;
    glowMesh.position.set(toX(cfg.x1) - glowOffset, centerY, midZ);
  }
  scene.add(glowMesh);

  const badgeY = cfg.yOff + cfg.h + (cfg.kind === 'tv' ? 0.16 : 0.13);
  const entry = {
    ...cfg,
    mat,
    ledMat,
    ledDot,
    glowMesh,
    glowMat,
    worldPos: new THREE.Vector3(midX, badgeY, midZ)
  };
  window.screensRegistry.push(entry);
  return entry;
}

// Möbel in Simons Zimmer (nach Skizze von Simon)
(function buildSimonFurniture() {
  function addBox(x1, y1, x2, y2, h, color, yOff = 0) {
    const geo = new THREE.BoxGeometry(Math.abs(x2 - x1) * SCALE, h, Math.abs(y2 - y1) * SCALE);
    const mesh = new THREE.Mesh(geo, new THREE.MeshStandardMaterial({ color, roughness: 0.8 }));
    mesh.position.set(toX((x1 + x2) / 2), yOff + h / 2, toZ((y1 + y2) / 2));
    mesh.castShadow = true;
    mesh.receiveShadow = true;
    groups.w1.details.add(mesh);
    return mesh;
  }

  // ---- SIMONS ZIMMER NORDWAND (NACH FOTO: SIDEBOARD + FERNSEHER + WEISSES 2x4 KALLAX-REGAL) ----
  // 1) Holz-/Weißes Sideboard links (px = 648..698, py = 118..132, Höhe = 0.85 m)
  addBox(648, 118, 698, 131.5, 0.82, 0xc8a97e);            // Eiche-Korpus
  addBox(647.5, 118, 698.5, 132.5, 0.03, 0xd6b88e, 0.82);  // Deckplatte (Höhe = 0.85 m)
  // Linkes Fach + weiße Schranktür mit silbernem Griff
  addBox(649, 120, 656, 131.8, 0.03, 0xb8976c, 0.42);      // Einlegeboden links
  addBox(656.5, 131.5, 667.5, 132.6, 0.76, 0xf8fafc, 0.04); // Weiße Tür
  addBox(657.5, 132.6, 658.3, 133.2, 0.16, 0x94a3b8, 0.60); // Silberner Türgriff
  // Mittlere 4 Holz-Schubladen mit silbernen Griffen
  for (let d = 0; d < 4; d++) {
    const dy = 0.04 + d * 0.19;
    addBox(668.2, 131.5, 683.5, 132.4, 0.17, 0xd2b48c, dy);
    addBox(673.5, 132.4, 678.2, 133.0, 0.025, 0x94a3b8, dy + 0.08);
  }
  // Rechtes offenes Regalfach mit Mittelboden
  addBox(684.5, 119, 697, 131.8, 0.03, 0xb8976c, 0.42);

  // Großer schwarzer Fernseher auf dem Sideboard (deutlich höher gemacht: px = 652..693, Oberkante ~1.82 m)
  addBox(666, 122.5, 679, 129.5, 0.025, 0x1e293b, 0.85);     // TV-Standfuß
  addBox(671, 125, 674, 127, 0.09, 0x1e293b, 0.875);         // TV-Hals (höher)
  addBox(652, 124.5, 693, 126.5, 0.86, 0x0f172a, 0.96);      // TV-Rahmen (großer, hoher Flatscreen)
  const simonTvMesh = addBox(653, 126.1, 692, 126.8, 0.80, 0x111827, 0.99); // TV-Display
  registerScreen({
    id: 'tv-simon',
    name: 'Fernseher (Simons Zimmer)',
    shortLabel: 'Fernseher',
    kind: 'tv',
    roomId: 'label-simon',
    apt: 'w1',
    mesh: simonTvMesh,
    x1: 653, y1: 126.1, x2: 692, y2: 126.8, h: 0.80, yOff: 0.99,
    faceDir: 'S',
    isOn: false
  });

  // 2) Weißes 2x4 KALLAX-Würfelregal rechts neben dem Sideboard (px = 699..728, py = 118..132, Höhe = 1.48 m)
  addBox(699, 118, 700.8, 132, 1.48, 0xf8fafc);            // Linke Seitenwand
  addBox(726.2, 118, 728, 132, 1.48, 0xf8fafc);            // Rechte Seitenwand
  addBox(712.8, 118.5, 714.2, 131.5, 1.44, 0xf1f5f9, 0.02); // Mittelwand vertikal
  for (let r = 0; r <= 4; r++) {
    addBox(699, 118, 728, 132, 0.035, 0xf8fafc, r * 0.361); // 5 horizontale Böden (4 Reihen)
  }
  // Untere 2 Reihen: Schachbrett-Muster aus schwarzen & weißen Kallax-Boxen
  addBox(701.2, 119, 712.4, 131.5, 0.31, 0x27272a, 0.04);  // Unten links: Dunkelgrau/Schwarz
  addBox(714.6, 119, 725.8, 131.5, 0.31, 0xf1f5f9, 0.04);  // Unten rechts: Weiß
  addBox(701.2, 119, 712.4, 131.5, 0.31, 0xf1f5f9, 0.40);  // 2. Reihe links: Weiß
  addBox(714.6, 119, 725.8, 131.5, 0.31, 0x27272a, 0.40);  // 2. Reihe rechts: Dunkelgrau/Schwarz
  // 3. Reihe: 2 schwarze Ordner rechts (mit weißem Rückenschild)
  addBox(715.5, 121, 719, 131, 0.29, 0x18181b, 0.76);      // Ordner 1
  addBox(716.2, 130.6, 718.3, 131.3, 0.14, 0xf8fafc, 0.86); // Rückenschild 1
  addBox(721, 121, 724.5, 131, 0.29, 0x18181b, 0.76);      // Ordner 2
  addBox(721.7, 130.6, 723.8, 131.3, 0.14, 0xf8fafc, 0.86); // Rückenschild 2

  // 3) Weißer Schreibtisch (wie auf dem Foto: weiße Platte, graues Gestell, PC-Tower unten links)
  const simonDeskTableMesh = addBox(730, 118, 752, 184, 0.05, 0xf8fafc, 0.70);        // Weiße Tischplatte
  [[730, 118], [749, 118], [730, 181], [749, 181]].forEach(([x, y]) => addBox(x, y, x + 3, y + 3, 0.70, 0x94a3b8));
  const simonPcTowerMesh = addBox(732, 120, 742, 131, 0.46, 0x18181b, 0.0);         // Schwarzer PC-Tower unter dem Tisch
  const simonPcFrontStrip = addBox(731.7, 121.5, 732.2, 123.0, 0.38, 0x38bdf8, 0.04); // Front-LED-Streifen am PC
  const simonPcPowerDot = addBox(731.7, 128.2, 732.3, 129.4, 0.025, 0x22c55e, 0.41);  // Power-LED am PC-Gehäuse
  const simonPcSideWin = addBox(733.2, 130.8, 740.8, 131.3, 0.34, 0x0ea5e9, 0.06);    // Leuchtendes PC-Seitenfenster
  const pcLedMat = new THREE.MeshStandardMaterial({
    color: 0x38bdf8,
    emissive: 0x0ea5e9,
    emissiveIntensity: 1.5,
    roughness: 0.2
  });
  simonPcFrontStrip.material = pcLedMat;
  simonPcFrontStrip.userData.origMat = pcLedMat;
  simonPcSideWin.material = pcLedMat;
  simonPcSideWin.userData.origMat = pcLedMat;

  const pcPowerBtnMat = new THREE.MeshBasicMaterial({ color: 0x22c55e });
  simonPcPowerDot.material = pcPowerBtnMat;
  simonPcPowerDot.userData.origMat = pcPowerBtnMat;

  window.simonPc = {
    isOn: true,
    roomId: 'label-simon',
    worldPos: new THREE.Vector3(toX(733), 0.28, toZ(121)),
    ledMat: pcLedMat,
    powerMat: pcPowerBtnMat,
    meshes: [simonPcTowerMesh, simonPcFrontStrip, simonPcPowerDot, simonPcSideWin]
  };
  addBox(716, 148, 728, 166, 0.06, 0x475569, 0.45);        // Sitz
  addBox(714, 148, 716, 166, 0.5, 0x475569, 0.45);         // Lehne (Westseite, Blick nach Ost)
  [[716, 148], [725, 148], [716, 163], [725, 163]].forEach(([x, y]) => addBox(x, y, x + 3, y + 3, 0.45, 0x1e293b));

  // 2 Monitore (Dual-Monitor-Setup) + Tastatur & Maus auf Simons Schreibtisch (Tischhöhe = 0.75 m)
  // Monitor 1 (links vom Stuhl aus gesehen: py = 136..155)
  addBox(745, 143, 749, 148, 0.02, 0x1e293b, 0.75);     // Standfuß 1
  addBox(747, 144.8, 748.5, 146.2, 0.14, 0x334155, 0.77); // Monitorarm 1
  addBox(745.5, 136, 747, 155, 0.36, 0x0f172a, 0.88);   // Gehäuse 1
  const simonMon1Mesh = addBox(745.1, 137, 745.6, 154, 0.32, 0x38bdf8, 0.90); // Leuchtendes Display 1
  registerScreen({
    id: 'mon-simon-1',
    name: 'Monitor 1 (Simons Zimmer)',
    shortLabel: 'Monitor 1',
    kind: 'monitor',
    roomId: 'label-simon',
    apt: 'w1',
    mesh: simonMon1Mesh,
    x1: 745.1, y1: 137, x2: 745.6, y2: 154, h: 0.32, yOff: 0.90,
    faceDir: 'W',
    isOn: true
  });

  // Monitor 2 (rechts vom Stuhl aus gesehen: py = 157..176)
  addBox(745, 164, 749, 169, 0.02, 0x1e293b, 0.75);     // Standfuß 2
  addBox(747, 165.8, 748.5, 167.2, 0.14, 0x334155, 0.77); // Monitorarm 2
  addBox(745.5, 157, 747, 176, 0.36, 0x0f172a, 0.88);   // Gehäuse 2
  const simonMon2Mesh = addBox(745.1, 158, 745.6, 175, 0.32, 0x38bdf8, 0.90); // Leuchtendes Display 2
  registerScreen({
    id: 'mon-simon-2',
    name: 'Monitor 2 (Simons Zimmer)',
    shortLabel: 'Monitor 2',
    kind: 'monitor',
    roomId: 'label-simon',
    apt: 'w1',
    mesh: simonMon2Mesh,
    x1: 745.1, y1: 158, x2: 745.6, y2: 175, h: 0.32, yOff: 0.90,
    faceDir: 'W',
    isOn: true
  });

  // Tastatur & Maus vor den Monitoren
  addBox(734, 149, 739, 161, 0.02, 0x1e293b, 0.75);     // Tastatur
  addBox(735, 164, 738, 166.5, 0.02, 0x334155, 0.75);   // Maus

  // 4) Simons 3-türiger Kleiderschrank (nach Foto, komplett geschlossen & aufgeräumt: px = 754..806, py = 118..142)
  addBox(754, 118, 806, 141.5, 0.06, 0xb8976c, 0.0);        // Holz-Sockelleiste unten
  addBox(754, 118, 806, 141.5, 2.01, 0xf1f5f9, 0.06);       // Weißer Schrank-Korpus
  addBox(753.8, 118, 806.2, 142.5, 0.03, 0xb8976c, 2.07);   // Holz-Abschlussleiste oben (Höhe = 2.10 m)

  // Drei Spalten (Links: 754.8..771.2, Mitte: 771.8..788.2, Rechts: 788.8..805.2)
  [[754.8, 771.2], [771.8, 788.2], [788.8, 805.2]].forEach(([cx1, cx2], idx) => {
    const midX = (cx1 + cx2) / 2;
    // Untere Reihe: 3 weiße große Schubladen/Fächer (yOff = 0.07..0.44)
    addBox(cx1, 141.5, cx2, 142.5, 0.37, 0xf8fafc, 0.07);
    addBox(midX - 3, 142.5, midX + 3, 143.1, 0.025, 0x94a3b8, 0.36); // Silberner Griff unten

    // Mittlere Reihe: 3 helle Eiche-Holzschubladen (yOff = 0.45..0.65)
    addBox(cx1, 141.5, cx2, 142.5, 0.20, 0xc8a97e, 0.45);
    addBox(midX - 3, 142.5, midX + 3, 143.1, 0.025, 0x94a3b8, 0.54); // Silberner Griff Mitte

    // Obere Reihe: 3 hohe weiße Schranktüren (yOff = 0.66..2.07, alle geschlossen)
    addBox(cx1, 141.5, cx2, 142.5, 1.40, 0xf8fafc, 0.66);

    // Vertikale silberne Türgriffe (Tür 1 rechts, Tür 2 links, Tür 3 links)
    const hx = (idx === 0) ? (cx2 - 1.4) : (cx1 + 1.4);
    addBox(hx - 0.35, 142.5, hx + 0.35, 143.2, 0.24, 0x94a3b8, 0.92);
  });

  // Großer Spiegel in der mittleren Schranktür (px = 774.2..785.8)
  const mirrorMesh = new THREE.Mesh(
    new THREE.BoxGeometry((785.8 - 774.2) * SCALE, 1.24, 0.015),
    new THREE.MeshStandardMaterial({ color: 0xbae6fd, roughness: 0.08, metalness: 0.85 })
  );
  mirrorMesh.position.set(toX(780), 0.66 + 0.70, toZ(142.6));
  groups.w1.details.add(mirrorMesh);

  // Bett IN Simons Zimmer (kürzer gemacht: px = 646..708, py = 188..241):
  // Rahmen, Matratze, Kopfteil an der Westwand, Kissen, blaue Decke
  addBox(646, 188, 708, 241, 0.5, 0x3f2a14);            // Bettrahmen (kürzer)
  addBox(650, 191, 705, 238, 0.18, 0xffffff, 0.5);      // Matratze
  addBox(646, 188, 651, 241, 1.05, 0x2b1b0c);           // Kopfteil (Westwand)
  addBox(653, 195, 669, 213, 0.14, 0xe2e8f0, 0.68);     // Kissen 1
  addBox(653, 216, 669, 234, 0.14, 0xe2e8f0, 0.68);     // Kissen 2
  addBox(672, 191, 705, 238, 0.12, 0x1d4ed8, 0.68);     // Blaue Decke

  // ---- DECKENLAMPE & ECHTES RAUMLICHT IN SIMONS ZIMMER (px = 725, py = 180) ----
  const lampX = toX(725);
  const lampZ = toZ(180);
  const lampMount = new THREE.Mesh(
    new THREE.CylinderGeometry(0.28, 0.30, 0.05, 32),
    new THREE.MeshStandardMaterial({ color: 0x1e293b, roughness: 0.4, metalness: 0.5 })
  );
  lampMount.position.set(lampX, 2.52, lampZ);
  groups.w1.details.add(lampMount);

  const lampBulbMat = new THREE.MeshStandardMaterial({
    color: 0xfef08a,
    emissive: 0xfacc15,
    emissiveIntensity: 1.35,
    roughness: 0.2
  });
  const lampBulb = new THREE.Mesh(
    new THREE.CylinderGeometry(0.25, 0.25, 0.03, 32),
    lampBulbMat
  );
  lampBulb.position.set(lampX, 2.49, lampZ);
  groups.w1.details.add(lampBulb);

  // Sichtbarer warmer Lichtkegel unter der Deckenlampe
  const lightConeMat = new THREE.MeshBasicMaterial({
    color: 0xfef08a,
    transparent: true,
    opacity: 0.11,
    side: THREE.DoubleSide,
    depthWrite: false
  });
  const lightConeMesh = new THREE.Mesh(
    new THREE.ConeGeometry(3.3, 2.46, 36, 1, true),
    lightConeMat
  );
  lightConeMesh.position.set(lampX, 1.25, lampZ);
  scene.add(lightConeMesh);

  // Warmer Lichtschein-Kreis auf dem Boden von Simons Zimmer
  const floorGlowCanvas = document.createElement('canvas');
  floorGlowCanvas.width = 256;
  floorGlowCanvas.height = 256;
  const fgCtx = floorGlowCanvas.getContext('2d');
  const grad = fgCtx.createRadialGradient(128, 128, 8, 128, 128, 126);
  grad.addColorStop(0, 'rgba(254, 240, 138, 0.52)');
  grad.addColorStop(0.55, 'rgba(250, 204, 21, 0.22)');
  grad.addColorStop(1, 'rgba(250, 204, 21, 0.0)');
  fgCtx.fillStyle = grad;
  fgCtx.fillRect(0, 0, 256, 256);
  const floorGlowTex = new THREE.CanvasTexture(floorGlowCanvas);
  const floorGlowMesh = new THREE.Mesh(
    new THREE.PlaneGeometry(7.2, 5.8),
    new THREE.MeshBasicMaterial({
      map: floorGlowTex,
      transparent: true,
      depthWrite: false
    })
  );
  floorGlowMesh.rotation.x = -Math.PI / 2;
  floorGlowMesh.position.set(lampX, 0.032, lampZ);
  scene.add(floorGlowMesh);

  // Echtes 3D-Punktlicht in Simons Zimmer
  const simonPointLight = new THREE.PointLight(0xffe49e, 1.65, 11.5, 1.35);
  simonPointLight.position.set(lampX, 2.38, lampZ);
  scene.add(simonPointLight);

  window.simonRoomLight = {
    isOn: true,
    roomId: 'label-simon',
    worldPos: new THREE.Vector3(lampX, 2.28, lampZ),
    bulbMat: lampBulbMat,
    coneMesh: lightConeMesh,
    floorGlowMesh,
    pointLight: simonPointLight,
    meshes: [lampMount, lampBulb, lightConeMesh, floorGlowMesh]
  };

  // ---- RGB TISCH-LED UNTER SIMONS SCHREIBTISCH (px = 730..752, py = 118..184, Höhe = 0.70 m) ----
  const ledStripMat = new THREE.MeshStandardMaterial({
    color: 0xa855f7,
    emissive: 0xa855f7,
    emissiveIntensity: 1.8,
    roughness: 0.2
  });
  // Umlaufender LED-Strip direkt unter der Tischplatte (y = 0.685 m)
  const stripBack = new THREE.Mesh(
    new THREE.BoxGeometry(1.5 * SCALE, 0.022, (183 - 119) * SCALE),
    ledStripMat
  );
  stripBack.position.set(toX(750.8), 0.685, toZ(151));
  groups.w1.details.add(stripBack);

  const stripFront = new THREE.Mesh(
    new THREE.BoxGeometry(1.5 * SCALE, 0.022, (183 - 119) * SCALE),
    ledStripMat
  );
  stripFront.position.set(toX(731.2), 0.685, toZ(151));
  groups.w1.details.add(stripFront);

  const stripLeft = new THREE.Mesh(
    new THREE.BoxGeometry((751 - 731) * SCALE, 0.022, 1.5 * SCALE),
    ledStripMat
  );
  stripLeft.position.set(toX(741), 0.685, toZ(119));
  groups.w1.details.add(stripLeft);

  const stripRight = new THREE.Mesh(
    new THREE.BoxGeometry((751 - 731) * SCALE, 0.022, 1.5 * SCALE),
    ledStripMat
  );
  stripRight.position.set(toX(741), 0.685, toZ(183));
  groups.w1.details.add(stripRight);

  // Leuchtendes Underglow-Volumen direkt unter dem Tisch (damit man die Farbe sofort optisch sieht!)
  const deskUnderGlowMat = new THREE.MeshBasicMaterial({
    color: 0xa855f7,
    transparent: true,
    opacity: 0.24,
    side: THREE.DoubleSide,
    depthWrite: false
  });
  const deskUnderGlowMesh = new THREE.Mesh(
    new THREE.BoxGeometry((751.5 - 730.5) * SCALE, 0.66, (183.5 - 118.5) * SCALE),
    deskUnderGlowMat
  );
  deskUnderGlowMesh.position.set(toX(741), 0.34, toZ(151));
  scene.add(deskUnderGlowMesh);

  // Leuchtender Farb-Pool auf dem Boden unter und vor dem Tisch
  const deskFloorGlowMat = new THREE.MeshBasicMaterial({
    color: 0xa855f7,
    transparent: true,
    opacity: 0.42,
    depthWrite: false
  });
  const deskFloorGlowMesh = new THREE.Mesh(
    new THREE.PlaneGeometry(1.75, 3.35),
    deskFloorGlowMat
  );
  deskFloorGlowMesh.rotation.x = -Math.PI / 2;
  deskFloorGlowMesh.position.set(toX(739), 0.034, toZ(151));
  scene.add(deskFloorGlowMesh);

  // Echtes 3D-Punktlicht unter dem Schreibtisch in der gewählten LED-Farbe
  const deskLedPointLight = new THREE.PointLight(0xa855f7, 1.8, 4.5, 1.4);
  deskLedPointLight.position.set(toX(739), 0.62, toZ(151));
  scene.add(deskLedPointLight);

  window.simonDeskLed = {
    isOn: true,
    colorHex: '#a855f7',
    colorName: 'Lila',
    brightness: 100,
    roomId: 'label-simon',
    worldPos: new THREE.Vector3(toX(741), 0.56, toZ(168)),
    stripMat: ledStripMat,
    underGlowMat: deskUnderGlowMat,
    underGlowMesh: deskUnderGlowMesh,
    floorGlowMat: deskFloorGlowMat,
    floorGlowMesh: deskFloorGlowMesh,
    pointLight: deskLedPointLight,
    meshes: [simonDeskTableMesh, stripBack, stripFront, stripLeft, stripRight, deskUnderGlowMesh, deskFloorGlowMesh]
  };

  // ---- ARBEITSZIMMER MÖBEL (px: 817..885, py: 226..325) ----
  // 1) Tisch + Stuhl + Monitor (links an der Westwand vor der Tür: px = 818..838, py = 236..290)
  addBox(818, 236, 838, 290, 0.06, 0xd6a96b, 0.70);     // Tischplatte
  [[818, 236], [835, 236], [818, 287], [835, 287]].forEach(([x, y]) =>
    addBox(x, y, x + 3, y + 3, 0.70, 0x6b4423)          // 4 Tischbeine
  );
  // Stuhl vor dem Tisch (auf der Ostseite mit Blick nach Westen zum Tisch)
  addBox(840, 254, 852, 272, 0.06, 0x475569, 0.45);     // Sitzfläche
  addBox(850, 254, 852, 272, 0.50, 0x475569, 0.45);     // Rückenlehne (Ostseite)
  [[840, 254], [849, 254], [840, 269], [849, 269]].forEach(([x, y]) =>
    addBox(x, y, x + 3, y + 3, 0.45, 0x1e293b)          // 4 Stuhlbeine
  );
  // Monitor + Tastatur & Maus auf dem Tisch im Arbeitszimmer (Tischhöhe = 0.76 m)
  addBox(820, 260.5, 824, 265.5, 0.02, 0x1e293b, 0.76); // Standfuß
  addBox(820.5, 262.3, 822, 263.7, 0.14, 0x334155, 0.78); // Monitorarm
  addBox(822, 253, 823.5, 273, 0.36, 0x0f172a, 0.89);   // Monitorgehäuse
  const arbeitMonMesh = addBox(823.4, 254, 823.9, 272, 0.32, 0x38bdf8, 0.91); // Leuchtendes Display (zeigt zum Stuhl)
  registerScreen({
    id: 'mon-arbeit',
    name: 'PC-Monitor (Arbeitszimmer)',
    shortLabel: 'Monitor',
    kind: 'monitor',
    roomId: 'label-arbeit',
    apt: 'w1',
    mesh: arbeitMonMesh,
    x1: 823.4, y1: 254, x2: 823.9, y2: 272, h: 0.32, yOff: 0.91,
    faceDir: 'E',
    isOn: true
  });
  addBox(830, 257, 835, 269, 0.02, 0x1e293b, 0.76);     // Tastatur
  addBox(831, 272, 834, 274.5, 0.02, 0x334155, 0.76);   // Maus

  // 2) Halbhohes Regal / Schrank (ca. 1 m hoch, oben am Fenster: px = 842..884, py = 227..244)
  addBox(842, 227, 884, 243, 0.96, 0xd6c7b2);           // Schrank-/Regal-Korpus (ca. 1m hoch)
  addBox(841.5, 227, 884.5, 244, 0.04, 0x8b5a2b, 0.96); // Holz-Deckplatte (Gesamthöhe = 1.00 m)
  addBox(843, 243, 862.5, 244, 0.86, 0xede4d3, 0.06);   // Schranktür links
  addBox(863.5, 243, 883, 244, 0.86, 0xede4d3, 0.06);   // Schranktür rechts
  addBox(860.5, 244, 861.5, 244.8, 0.18, 0x334155, 0.48); // Griff links
  addBox(864.5, 244, 865.5, 244.8, 0.18, 0x334155, 0.48); // Griff rechts

  // 3) Waschmaschine (Rot markiert, unten rechts in der Ecke: px = 864..884, py = 304..324)
  addBox(864, 304, 884, 324, 0.85, 0xf8fafc);           // Weißer Korpus
  addBox(863.5, 304, 884, 324, 0.04, 0xe2e8f0, 0.85);   // Deckplatte
  addBox(863, 305, 864, 323, 0.14, 0xcbd5e1, 0.70);     // Bedienblende vorne (Westseite)
  addBox(862.5, 313, 863.2, 315, 0.05, 0x475569, 0.74); // Drehregler
  addBox(862.6, 306, 863.2, 311, 0.08, 0x94a3b8, 0.73); // Waschmittelschublade
  // Bullauge (Waschmaschinentrommel vorne an der Westseite)
  const drumRing = new THREE.Mesh(
    new THREE.CylinderGeometry(0.24, 0.24, 0.05, 24),
    new THREE.MeshStandardMaterial({ color: 0x334155, roughness: 0.4, metalness: 0.5 })
  );
  drumRing.rotation.z = Math.PI / 2;
  drumRing.position.set(toX(863.5), 0.42, toZ(314));
  groups.w1.details.add(drumRing);

  const drumGlass = new THREE.Mesh(
    new THREE.CylinderGeometry(0.18, 0.18, 0.06, 24),
    new THREE.MeshStandardMaterial({ color: 0x0f172a, roughness: 0.15, metalness: 0.8 })
  );
  drumGlass.rotation.z = Math.PI / 2;
  drumGlass.position.set(toX(863.4), 0.42, toZ(314));
  groups.w1.details.add(drumGlass);

  // ---- FLUR MÖBEL & WANDBILDER (px: 717..807, py: 253..325) ----
  // 1) Regal 1 im Flur (links im Bild an der Wand zum Arbeitszimmer: px = 793..806, py = 254..288)
  addBox(793, 254, 806, 288, 1.55, 0x8b5a2b);             // Regal-Korpus
  for (let s = 0; s < 4; s++) {
    addBox(792.5, 255, 805.5, 287, 0.04, 0xb47b44, 0.34 + s * 0.36); // 4 Regalböden
  }

  // 2) Regal 2 im Flur (unten rechts im Bild an der Wand zu Simons Zimmer: px = 723..748, py = 254..266)
  addBox(723, 254, 748, 266, 1.35, 0x8b5a2b);             // Regal-Korpus
  for (let s = 0; s < 3; s++) {
    addBox(723.5, 254.5, 747.5, 266.5, 0.04, 0xb47b44, 0.36 + s * 0.38); // 3 Regalböden
  }

  // 3) Drei Wandbilder im Flur (Gelb markiert)
  // Bild 1 (Südwand py = 325, links im Bild: px = 776..794)
  addBox(776, 323.5, 794, 325, 0.72, 0x1e293b, 1.25);     // Bilderrahmen
  addBox(777.5, 323.1, 792.5, 324.2, 0.60, 0xfef3c7, 1.31); // Passepartout
  addBox(779, 322.9, 791, 323.8, 0.46, 0x38bdf8, 1.38);   // Motiv (Blau)

  // Bild 2 (Südwand py = 325, rechts im Bild: px = 734..752)
  addBox(734, 323.5, 752, 325, 0.72, 0x1e293b, 1.25);     // Bilderrahmen
  addBox(735.5, 323.1, 750.5, 324.2, 0.60, 0xfef3c7, 1.31); // Passepartout
  addBox(737, 322.9, 749, 323.8, 0.46, 0xf59e0b, 1.38);   // Motiv (Warmgelb/Orange)

  // Bild 3 (Westwand zur Abstellkammer px = 717: py = 262..280)
  addBox(717, 262, 718.5, 280, 0.72, 0x1e293b, 1.25);     // Bilderrahmen
  addBox(717.8, 263.5, 718.9, 278.5, 0.60, 0xfef3c7, 1.31); // Passepartout
  addBox(718.2, 265, 719.1, 277, 0.46, 0x34d399, 1.38);   // Motiv (Grün)

  // ---- MARTINS ZIMMER MÖBEL (px: 765..939, py: 335..437) ----
  // 1) Bett im 45°-Erker (Rot markiert: schräg an der oberen Erker-Fensterwand)
  const martinBedGroup = new THREE.Group();
  martinBedGroup.position.set(toX(937.5), 0, toZ(387.5));
  martinBedGroup.rotation.y = -Math.PI / 4;
  groups.w1.details.add(martinBedGroup);

  function addMartinBedPart(lx1, lz1, lx2, lz2, h, color, yOff = 0) {
    const geo = new THREE.BoxGeometry(Math.abs(lx2 - lx1) * SCALE, h, Math.abs(lz2 - lz1) * SCALE);
    const mesh = new THREE.Mesh(geo, new THREE.MeshStandardMaterial({ color, roughness: 0.8 }));
    mesh.position.set(((lx1 + lx2) / 2) * SCALE, yOff + h / 2, ((lz1 + lz2) / 2) * SCALE);
    mesh.castShadow = true;
    mesh.receiveShadow = true;
    martinBedGroup.add(mesh);
  }
  addMartinBedPart(-52, 1, 0, 33, 0.48, 0x3f2a14);          // Bettrahmen (45° im Erker)
  addMartinBedPart(-50, 2.5, -2, 31.5, 0.18, 0xffffff, 0.48); // Matratze
  addMartinBedPart(-3, 1, 0, 33, 1.02, 0x2b1b0c);           // Kopfteil (an der rechten Erkerwand)
  addMartinBedPart(-15, 5, -4, 29, 0.14, 0xe2e8f0, 0.66);   // Kissen
  addMartinBedPart(-49, 2.5, -17, 31.5, 0.12, 0xdc2626, 0.66); // Bettdecke (Rot/Warmrot)

  // 2) Wand-Fernseher (Blau markiert, hängt links oben an der Wand py = 335: px = 852..882)
  addBox(852, 335, 882, 336.4, 0.62, 0x0f172a, 1.12);       // TV-Gehäuse an der Wand
  const martinTvMesh = addBox(853.5, 336.1, 880.5, 336.7, 0.54, 0x0284c7, 1.16); // TV-Bildschirm
  registerScreen({
    id: 'tv-martin',
    name: 'Fernseher (Martins Zimmer)',
    shortLabel: 'Fernseher',
    kind: 'tv',
    roomId: 'label-martin',
    apt: 'w1',
    mesh: martinTvMesh,
    x1: 853.5, y1: 336.1, x2: 880.5, y2: 336.7, h: 0.54, yOff: 1.16,
    faceDir: 'S',
    isOn: true
  });

  // 3) Klamottenschrank (Lila markiert, an der Wand py = 335: px = 778..834, py = 336..358)
  addBox(778, 336, 834, 358, 2.10, 0xe7e0d2);               // Schrank-Korpus
  addBox(780, 358, 805.5, 359, 1.95, 0xf5f0e6, 0.08);       // Tür links
  addBox(806.5, 358, 832, 359, 1.95, 0xf5f0e6, 0.08);       // Tür rechts
  addBox(803.5, 359, 804.5, 359.8, 0.48, 0x334155, 0.90);   // Griff links
  addBox(807.5, 359, 808.5, 359.8, 0.48, 0x334155, 0.90);   // Griff rechts

  // 4) Schreibtisch / Tisch + Monitor & PC (Gelb/Orange markiert, an der Westwand px = 765: py = 382..434)
  addBox(766, 382, 788, 434, 0.06, 0xd6a96b, 0.70);         // Tischplatte (Höhe 0.76 m)
  [[766, 382], [785, 382], [766, 431], [785, 431]].forEach(([x, y]) =>
    addBox(x, y, x + 3, y + 3, 0.70, 0x6b4423)              // 4 Tischbeine
  );
  // PC-Tower oben auf dem Tisch (am oberen Ende: px = 768..780, py = 384..393)
  addBox(768, 384, 780, 393, 0.44, 0x111827, 0.76);         // Schwarzes PC-Gehäuse auf dem Tisch
  addBox(769.5, 392.8, 778.5, 393.3, 0.36, 0x1e293b, 0.80); // Seitliches Sichtfenster
  addBox(779.8, 385.5, 780.4, 391.5, 0.38, 0x38bdf8, 0.79); // Dezenter blauer Front-Leuchtstreifen
  // Monitor auf dem Tisch (px = 769..774, py = 400..422, Blick Richtung Raum/+px)
  addBox(769, 408, 774, 414, 0.02, 0x1e293b, 0.76);         // Monitor-Standfuß
  addBox(769.8, 410.2, 771.2, 411.8, 0.14, 0x334155, 0.78); // Monitor-Arm
  addBox(770.5, 400, 772.2, 422, 0.38, 0x0f172a, 0.89);     // Monitor-Gehäuse
  const martinMonMesh = addBox(772.0, 401, 772.6, 421, 0.34, 0x38bdf8, 0.91); // Leuchtendes Monitor-Display
  registerScreen({
    id: 'mon-martin',
    name: 'PC-Monitor (Martins Zimmer)',
    shortLabel: 'Monitor',
    kind: 'monitor',
    roomId: 'label-martin',
    apt: 'w1',
    mesh: martinMonMesh,
    x1: 772.0, y1: 401, x2: 772.6, y2: 421, h: 0.34, yOff: 0.91,
    faceDir: 'E',
    isOn: true
  });
  // Tastatur & Maus vor dem Monitor
  addBox(778, 404, 783, 416, 0.02, 0x1e293b, 0.76);         // Tastatur
  addBox(779, 419, 782, 421.5, 0.02, 0x334155, 0.76);       // Maus

  // 5) Sessel (Grün markiert, gemütlicher Sessel im Raum: px = 805..826, py = 403..424)
  addBox(805, 403, 826, 424, 0.40, 0x15803d);               // Sessel-Sitzbasis
  addBox(808, 406, 823, 421, 0.12, 0x22c55e, 0.40);         // Sitzpolster
  addBox(805, 420, 826, 424, 0.82, 0x166534);               // Rückenlehne
  addBox(805, 403, 809, 424, 0.62, 0x166534);               // Armlehne unten
  addBox(822, 403, 826, 424, 0.62, 0x166534);               // Armlehne oben

  // 6) Kommode (Grau markiert, rechts an der Wand py = 437: px = 836..882, py = 418..436)
  addBox(836, 419, 882, 436, 0.88, 0x94a3b8);               // Kommoden-Korpus
  addBox(835.5, 418.5, 882.5, 436.5, 0.04, 0xcbd5e1, 0.88); // Deckplatte (0.92 m hoch)
  for (let d = 0; d < 3; d++) {
    const dy = 0.08 + d * 0.26;
    addBox(838, 418, 880, 419, 0.22, 0xe2e8f0, dy);         // 3 Schubladen-Fronten
    addBox(856, 417.2, 862, 418, 0.04, 0x334155, dy + 0.10); // Schubladen-Griffe
  }

  // ---- ABSTELLKAMMER REGALBÖDEN IN L-FORM (px: 646..707, py: 253..280) ----
  for (let s = 0; s < 5; s++) {
    const sy = 0.35 + s * 0.38;
    // Langer Schenkel an der Nordwand (py = 253..264, px = 646..707)
    addBox(646, 253, 707, 264, 0.04, 0xd6a96b, sy);
    // Kurzer Schenkel im Winkel an der Ostwand (px = 696..707, py = 264..280)
    addBox(696, 264, 707, 280, 0.04, 0xd6a96b, sy);
  }

  // ---- NEUES BAD EINRICHTUNG (px: 689..755, py: 367..437) ----
  // 1) Dusche (Grün markiert: oben links in der Nische px = 689..709, py = 367..388)
  addBox(689, 367, 709, 388, 0.07, 0xf8fafc, 0.0);            // Flache weiße Duschwanne
  addBox(690.5, 368.5, 707.5, 386.5, 0.015, 0xe2e8f0, 0.07);  // Duschfläche innen
  addBox(697.5, 370, 700.5, 372, 0.02, 0x64748b, 0.075);      // Silberne Ablaufrinne
  // Duschstange & Regendusche an der Nordwand der Nische (py = 367.5)
  addBox(698.6, 367.2, 699.4, 368.2, 1.15, 0x94a3b8, 0.85);   // Vertikale Duschstange
  addBox(696.5, 367.2, 701.5, 368.8, 0.05, 0x64748b, 1.05);   // Duscharmatur / Thermostat
  addBox(695.5, 368, 702.5, 375, 0.03, 0x94a3b8, 1.98);       // Regenduschkopf oben
  // Gläserne Duschabtrennung vorne (py = 387.5) mit silbernem Rahmen & Griff
  addBox(689, 387.2, 690, 388.2, 1.95, 0x94a3b8, 0.07);       // Alu-Profil links
  addBox(708, 387.2, 709, 388.2, 1.95, 0x94a3b8, 0.07);       // Alu-Profil rechts
  addBox(702, 386.6, 703, 388.8, 0.22, 0x64748b, 0.95);       // Türgriff an der Glaswand
  const showerGlass = new THREE.Mesh(
    new THREE.BoxGeometry((708 - 690) * SCALE, 1.92, 0.02),
    mats.w1.glass
  );
  showerGlass.position.set(toX(699), 0.07 + 1.92 / 2, toZ(387.7));
  groups.w1.details.add(showerGlass);

  // 2) Klo / WC (Orange markiert: unten links an der Südwand py = 437, px = 693..706)
  addBox(693, 430, 706, 437, 0.92, 0xe2e8f0, 0.0);            // Vorwandelement / Spülkasten-Block
  addBox(692.8, 429.8, 706.2, 437, 0.03, 0xf8fafc, 0.92);     // Ablagefläche auf dem Spülkasten
  addBox(697, 429.5, 702, 430.2, 0.14, 0x94a3b8, 0.68);       // Silberne Spültaste (Drückerplatte)
  addBox(695, 416, 704, 430, 0.38, 0xffffff, 0.04);           // Weiße Keramik-WC-Schüssel
  addBox(694.5, 415.5, 704.5, 429.5, 0.04, 0xf8fafc, 0.42);   // WC-Brille / Klodeckel

  // 3) Hoher Badschrank (Dunkellila markiert: Mitte der Südwand py = 437, px = 711..723)
  addBox(711, 424, 723, 437, 1.92, 0xc8a97e, 0.0);            // Hoher Schrank-Korpus (Eiche/Holzoptik)
  addBox(711.6, 423.2, 722.4, 424, 0.86, 0xf8fafc, 0.06);     // Untere weiße Schranktür
  addBox(711.6, 423.2, 722.4, 424, 0.92, 0xf8fafc, 0.96);     // Obere weiße Schranktür
  addBox(720.5, 422.5, 721.3, 423.2, 0.18, 0x94a3b8, 0.70);   // Silberner Griff unten
  addBox(720.5, 422.5, 721.3, 423.2, 0.18, 0x94a3b8, 1.04);   // Silberner Griff oben

  // 4) Waschbecken mit Unterschrank & Spiegel (Helllila markiert: geradeaus vom Eingang, px = 727..753, py = 422..437)
  addBox(727, 423, 753, 437, 0.76, 0xc8a97e, 0.06);           // Waschtisch-Unterschrank (schwebend)
  addBox(728, 422.2, 752, 423, 0.33, 0xf8fafc, 0.09);         // Untere Schublade (Weiß)
  addBox(728, 422.2, 752, 423, 0.33, 0xf8fafc, 0.45);         // Obere Schublade (Weiß)
  addBox(737, 421.5, 743, 422.2, 0.025, 0x94a3b8, 0.36);      // Griff unten
  addBox(737, 421.5, 743, 422.2, 0.025, 0x94a3b8, 0.72);      // Griff oben
  // Weißes Keramik-Waschbecken oben drauf
  addBox(726.5, 422, 753.5, 437, 0.08, 0xffffff, 0.82);       // Waschtischplatte / Beckenrand
  addBox(731, 424.5, 749, 433.5, 0.03, 0xe2e8f0, 0.885);      // Waschbecken-Mulde innen
  // Silberner Wasserhahn (Armatur)
  addBox(739.3, 434, 740.7, 435.8, 0.14, 0x94a3b8, 0.90);     // Hahn-Basis
  addBox(739.4, 431.5, 740.6, 435.5, 0.025, 0x94a3b8, 1.02);  // Hahn-Auslauf nach vorne
  // Großer Badspiegel an der Südwand über dem Waschbecken
  const badMirror = new THREE.Mesh(
    new THREE.BoxGeometry((751 - 729) * SCALE, 0.78, 0.02),
    new THREE.MeshStandardMaterial({ color: 0xbae6fd, roughness: 0.08, metalness: 0.85 })
  );
  badMirror.position.set(toX(740), 1.08 + 0.78 / 2, toZ(436.4));
  groups.w1.details.add(badMirror);

  // ---- VORDERER FLUR: JACKENHAKEN (ORANGE) & RUNDES ECKREGAL MIT TELEFON (GRÜN) ----
  // 1) Wandgarderobe / Hakenleiste für Jacken (Orange markiert: an der Bad-Außenwand px = 679, py = 374..416)
  addBox(677.6, 374, 679, 416, 0.12, 0xc8a97e, 1.56);           // Holz-Garderobenleiste an der Wand
  addBox(673.5, 374, 679, 416, 0.03, 0xd6b88e, 1.68);           // Kleine Hutablage oben an der Leiste
  // 5 silberne Doppel-Jackenhaken + aufgehängte Jacken
  [378, 386.5, 395, 403.5, 412].forEach((hy, idx) => {
    addBox(675.2, hy - 0.6, 677.6, hy + 0.6, 0.03, 0x94a3b8, 1.59); // Haken-Arm
    addBox(675.0, hy - 0.6, 675.8, hy + 0.6, 0.07, 0x94a3b8, 1.59); // Haken-Spitze nach oben
    // Aufgehängte Jacken an 3 der 5 Haken
    if (idx === 0 || idx === 2 || idx === 3) {
      const jColors = [0x1e3a8a, 0xb91c1c, 0x334155];
      const jCol = jColors[idx === 0 ? 0 : (idx === 2 ? 1 : 2)];
      addBox(673.2, hy - 3.2, 677.4, hy + 3.2, 0.64, jCol, 0.94);   // Jacke an der Wand
    }
  });

  // 2) Rundes Eckregal / Halterung mit Telefon drauf (Grün markiert: in der Ecke px = 646..660, py = 297..311)
  const cornerRadius = 13.5 * SCALE; // ~0.54 m Radius für das runde Viertelkreis-Eckregal
  const shelfMatWood = new THREE.MeshStandardMaterial({ color: 0xc8a97e, roughness: 0.75 });
  [0.56, 0.88].forEach(sy => {
    const quarterGeo = new THREE.CylinderGeometry(cornerRadius, cornerRadius, 0.035, 24, 1, false, 0, Math.PI / 2);
    const quarterMesh = new THREE.Mesh(quarterGeo, shelfMatWood);
    quarterMesh.position.set(toX(646), sy, toZ(297));
    quarterMesh.castShadow = true;
    quarterMesh.receiveShadow = true;
    groups.w1.details.add(quarterMesh);
  });
  // Vertikale Eck-/Wandhalterung für das runde Regal
  addBox(646, 297, 648.5, 299.5, 0.42, 0x64748b, 0.48);

  // Festnetz-Telefon oben auf dem runden Eckregal (bei px = 649..655, py = 300..306, yOff = 0.90 m)
  addBox(649.5, 300.5, 655, 306, 0.04, 0x1e293b, 0.90);         // Telefon-Basisstation (Anthrazit/Schwarz)
  addBox(651.5, 301.8, 654.2, 304.8, 0.015, 0x38bdf8, 0.94);    // Kleines leuchtendes Telefon-Display
  addBox(649.8, 300.8, 651.2, 305.6, 0.035, 0x0f172a, 0.94);    // Aufgelegter Telefonhörer / Mobilteil
  addBox(650.0, 301.2, 651.0, 302.6, 0.14, 0x0f172a, 0.94);     // Stehendes Schnurlos-Mobilteil in Ladeschale
})();

// 3. WOHNUNG 2 — UNTEN / SÜD (px: 486..954, py: 372..704)
(function buildWohnung2() {
  // West Room & Entrance Foyer (px: 486..638, py: 372..498)
  addRectFloor('w2', 486, 372, 638, 498);
  // Central Dining / Hallway (px: 538..686, py: 445..582)
  addRectFloor('w2', 538, 498, 686, 582);
  addRectFloor('w2', 638, 445, 686, 498);
  // WOHNZIMMER (px: 686..893, py: 445..582 – bündige Ostfassade bei px = 893!)
  addRectFloor('w2', 686, 445, 893, 582);
  // Bottom-South Bedrooms & Baths (px: 538..893, py: 582..704)
  addRectFloor('w2', 538, 582, 893, 704);
  // South-East Balcony Slab (px: 893..954, py: 445..704)
  addPolyFloor('w2', [
    [893, 445],
    [954, 506],
    [954, 704],
    [893, 704]
  ], true);

  // South-East Balcony Railing mit exakten Gehrungsecken bei (954, 506) und (954, 704) sowie bündigem Wandanschluss
  const r22_5 = 2.5 * (Math.SQRT2 - 1); // 1.0355 px für 45°-Knick bei Geländerstärke 5
  addBalconyRailing('w2', 898, 450, 954, 506, 5, 1.02, [4, [r22_5, -r22_5]]);
  addBalconyRailing('w2', 954, 506, 954, 704, 5, 1.02, [[r22_5, -r22_5], [2.5, -2.5]]);
  addBalconyRailing('w2', 954, 704, 901, 704, 5, 1.02, [[2.5, -2.5], 2]);

  // North & West Walls of West Room & Entrance Foyer (durchgehend Stärke 16, schließt bei px = 630 bündig an W1 an)
  addWall('w2', 486, 372, 630, 372, 16, [
    { type: 'door', from: 590, to: 624, swingDir: 1, hinge: 'end' }
  ], H_WALL, [8, 0]);
  addWall('w2', 486, 372, 486, 498, 16, [
    { type: 'window', from: 412, to: 468 }
  ]);
  addWall('w2', 486, 498, 575, 498, 16, [], H_WALL, [8, 0]);
  addWall('w2', 570, 372, 570, 498, 10, [
    { type: 'door', from: 462, to: 490, swingDir: -1, hinge: 'end' }
  ], H_WALL, [-8, -8]);
  addWall('w2', 570, 445, 630, 445, 16, [
    { type: 'door', from: 594, to: 624, swingDir: 1, hinge: 'start' }
  ], H_WALL, [-5, 0]);

  // Durchgehende Westwand von Esszimmer & Schlafzimmer (px = 538, py: 498..704, durchgehend Stärke 16 ohne Versatz)
  addWall('w2', 538, 498, 538, 704, 16, [
    { type: 'window', from: 510, to: 572 },
    {
      type: 'window',
      from: 612,
      to: 672,
      shutterId: 'shutter-schlaf',
      shutterName: 'Schlafzimmer',
      roomId: 'label-schlaf',
      initialPercent: 40
    }
  ], H_WALL, [-8, 8]);

  // WOHNZIMMER (px: 686..893, py: 445..582)
  // Left Wall of Wohnzimmer (px = 686, py: 445..582) mit Türeingang unten (Grün markiert: py = 536..570)
  addWall('w2', 686, 445, 686, 582, 10, [
    { type: 'door', from: 536, to: 570, swingDir: 1, hinge: 'end' }
  ], H_WALL, [-8, -8]);

  // Durchgehende Ostfassade von Wohnung 2 (px = 893, py: 445..704) – oben mit Gehrung zum Erker (893, 445), unten bündig bei (893, 704)!
  const m22_5_w2 = 8 * (Math.SQRT2 - 1);
  addWall('w2', 893, 445, 893, 704, 16, [
    { type: 'window', from: 469, to: 532, lowSill: true },
    { type: 'door', from: 533, to: 567, swingDir: -1, hinge: 'end' },
    {
      type: 'door',
      from: 604,
      to: 638,
      swingDir: -1,
      hinge: 'start',
      shutterId: 'shutter-melanie-door',
      shutterName: 'Melaniers Zimmer (Balkontür)',
      shutterLabel: 'Rollladen Tür',
      roomId: 'label-melanie',
      initialPercent: 25
    },
    {
      type: 'window',
      from: 640,
      to: 686,
      lowSill: true,
      shutterId: 'shutter-melanie-win',
      shutterName: 'Melaniers Zimmer (Fenster)',
      shutterLabel: 'Rollladen Fenster',
      roomId: 'label-melanie',
      initialPercent: 25
    }
  ], H_WALL, [[-m22_5_w2, m22_5_w2], 8]);

  // Horizontal Wall at py = 582 (px: 538..893) – stößt innen bündig an die West- und Ostwand (px = 538 & px = 893)
  addWall('w2', 538, 582, 893, 582, 16, [
    { type: 'door', from: 638, to: 672, swingDir: -1, hinge: 'end' }
  ], H_WALL, [-8, -8]);

  // Bottom-South Outer Wall (py = 704, px: 538..893)
  addWall('w2', 538, 704, 893, 704, 16, [
    { type: 'window', from: 642, to: 668 },
    { type: 'window', from: 684, to: 716 }
  ], H_WALL, [8, 8]);

  // Bottom-South Interior Walls (SW Bedroom, WC, Bath, SE Bedroom – alle einheitlich Stärke 10 und bündig)
  addWall('w2', 634, 582, 634, 704, 10, [
    { type: 'door', from: 592, to: 617, swingDir: -1, hinge: 'start' }
  ], H_WALL, [-8, -8]);
  addWall('w2', 748, 582, 748, 704, 10, [
    { type: 'door', from: 592, to: 617, swingDir: 1, hinge: 'start' }
  ], H_WALL, [-8, -8]);
  addWall('w2', 634, 622, 748, 622, 10, [
    { type: 'door', from: 641, to: 667, swingDir: 1, hinge: 'start' },
    { type: 'door', from: 684, to: 716, swingDir: 1, hinge: 'end' }
  ], H_WALL, [-5, -5]);
  addWall('w2', 674, 622, 674, 704, 10, [], H_WALL, [-5, -8]);

  // Hilfsfunktion für Möbel in Wohnung 2
  function addBoxW2(x1, y1, x2, y2, h, color, yOff = 0) {
    const geo = new THREE.BoxGeometry(Math.abs(x2 - x1) * SCALE, h, Math.abs(y2 - y1) * SCALE);
    const mesh = new THREE.Mesh(geo, new THREE.MeshStandardMaterial({ color, roughness: 0.8 }));
    mesh.position.set(toX((x1 + x2) / 2), yOff + h / 2, toZ((y1 + y2) / 2));
    mesh.castShadow = true;
    mesh.receiveShadow = true;
    groups.w2.details.add(mesh);
    return mesh;
  }

  // ---- KÜCHE MÖBEL & GERÄTE (px: 494..565, py: 380..490) ----
  // 1) L-förmige Küchenzeile / Arbeitsfläche (Rot markiert: Nordwand px = 494..544, py = 380..396 + Westwand px = 494..510, py = 396..490)
  // Unterschränke (Weiß, Höhe 0.86 m) + Arbeitsplatte in Eiche/Holzoptik (Höhe 0.04 m auf yOff = 0.86 m)
  addBoxW2(494, 380, 544, 396, 0.86, 0xf8fafc, 0.0);            // Unterschränke Nordschenkel
  addBoxW2(494, 380, 544.5, 396.8, 0.04, 0xb88655, 0.86);       // Arbeitsplatte Nordschenkel (0.90 m)
  addBoxW2(494, 396, 510, 490, 0.86, 0xf8fafc, 0.0);            // Unterschränke Westschenkel (lang)
  addBoxW2(494, 396, 510.8, 490, 0.04, 0xb88655, 0.86);         // Arbeitsplatte Westschenkel (0.90 m)
  // Spülbecken + Wasserhahn auf dem langen Westschenkel vor dem Fenster (Grau markiert: px = 496..508.5, py = 452..470)
  addBoxW2(496, 452, 508.5, 470, 0.015, 0x94a3b8, 0.90);          // Edelstahl-Spüle Rand
  addBoxW2(498, 454, 507, 468, 0.02, 0x475569, 0.905);            // Spülbecken-Mulde
  addBoxW2(495.5, 460.2, 497.2, 461.8, 0.18, 0x64748b, 0.91);     // Küchenarmatur (Wasserhahn)
  addBoxW2(496.5, 460.3, 501.0, 461.7, 0.025, 0x64748b, 1.07);    // Hahn-Auslauf

  // 2) Herdplatte / Kochfeld + Backofen (Blau markiert: auf dem linken Schenkel bei px = 496..509, py = 408..432)
  addBoxW2(496, 408, 509, 432, 0.018, 0x0f172a, 0.90);          // Schwarzes Ceran-/Induktionskochfeld
  // 4 Kochfelder (Platten) auf dem Herd
  [[499, 412], [504.5, 412], [499, 423], [504.5, 423]].forEach(([hx, hy]) => {
    addBoxW2(hx, hy, hx + 3.2, hy + 5, 0.008, 0x334155, 0.915);
  });
  // Eingebauter Backofen vorne unter dem Kochfeld (an px = 510)
  addBoxW2(510, 409, 510.8, 431, 0.58, 0x1e293b, 0.16);         // Backofen-Front
  addBoxW2(510.2, 411.5, 511.0, 428.5, 0.36, 0x090d16, 0.24);   // Backofen-Sichtfenster
  addBoxW2(510.8, 412, 511.5, 428, 0.03, 0x94a3b8, 0.68);       // Silberner Backofen-Griff

  // Schranktüren & Schubladen-Griffe entlang der langen Küchenzeile
  [438, 456, 474].forEach(sy => {
    addBoxW2(510.8, sy, 511.4, sy + 8, 0.025, 0x94a3b8, 0.76);  // Griffe oben
    addBoxW2(510.8, sy, 511.4, sy + 8, 0.025, 0x94a3b8, 0.48);  // Griffe Mitte
  });

  // 3) Kühlschrank oben rechts in der Ecke (Orange markiert: px = 545..564.5, py = 380.5..399)
  addBoxW2(545, 380.5, 564.5, 398.5, 1.88, 0xcbd5e1, 0.0);      // Hoher Edelstahl-/Silber-Kühlschrank
  addBoxW2(545.5, 398.5, 564, 399.5, 0.68, 0xe2e8f0, 0.05);     // Gefrierfach-Tür unten
  addBoxW2(545.5, 398.5, 564, 399.5, 1.08, 0xe2e8f0, 0.76);     // Kühlschrank-Tür oben
  addBoxW2(546.8, 399.5, 547.8, 400.3, 0.36, 0x64748b, 0.32);   // Griff unten
  addBoxW2(546.8, 399.5, 547.8, 400.3, 0.48, 0x64748b, 0.95);   // Griff oben

  // 4) Tisch direkt an der rechten Wand (Grün markiert: px = 542..565, py = 412..456)
  addBoxW2(542, 412, 565, 456, 0.05, 0xd6a96b, 0.70);           // Tischplatte direkt an der Wand (Höhe 0.75 m)
  [[543, 413], [561.5, 413], [543, 452.5], [561.5, 452.5]].forEach(([x, y]) =>
    addBoxW2(x, y, x + 2.8, y + 2.8, 0.70, 0x6b4423)            // 4 Tischbeine
  );

  // 5) Schaltbare Steckdose (Smart-Plug) an der rechten Küchenwand am roten Punkt (px = 564.4, py = 455, y = 0.58 m)
  const sockFrame = addBoxW2(564.2, 453.5, 565.0, 456.5, 0.11, 0xf8fafc, 0.52);   // Weißer Steckdosen-Rahmen an der Ostwand
  const sockPlug = addBoxW2(563.5, 454.0, 564.3, 456.0, 0.085, 0xe2e8f0, 0.532); // Smart-Plug Zwischenstecker
  const kuechePlugLedMat = new THREE.MeshStandardMaterial({
    color: 0x22c55e,
    emissive: 0x22c55e,
    emissiveIntensity: 1.6,
    roughness: 0.2
  });
  const kuechePlugLedMesh = new THREE.Mesh(
    new THREE.BoxGeometry(0.014, 0.025, 0.025),
    kuechePlugLedMat
  );
  kuechePlugLedMesh.position.set(toX(563.4), 0.575, toZ(455.0));
  groups.w2.details.add(kuechePlugLedMesh);

  window.kuecheSocket = {
    isOn: true,
    roomId: 'label-kueche',
    worldPos: new THREE.Vector3(toX(562.5), 0.62, toZ(455.0)),
    ledMat: kuechePlugLedMat,
    meshes: [sockFrame, sockPlug, kuechePlugLedMesh]
  };

  // ---- EINGANGSFLUR GARDEROBE / KLEIDERSCHRANK (Grün markiert: px = 575.5..590, py = 381..436) ----
  addBoxW2(575.5, 381, 589.5, 436, 0.06, 0xb8976c, 0.0);        // Holz-Sockelleiste unten
  addBoxW2(575.5, 381, 589.5, 436, 1.96, 0xf1f5f9, 0.06);       // Weißer Schrank-Korpus
  addBoxW2(575.5, 380.8, 590.2, 436.2, 0.03, 0xb8976c, 2.02);   // Holz-Abschlusskante oben (Höhe = 2.05 m)
  // 3 Schranktüren an der Ostseite (Richtung Flur/Durchgang, px = 589.5..590.3)
  [[382, 399.5], [400.3, 417.7], [418.5, 435]].forEach(([cy1, cy2], idx) => {
    // Untere Schuh-/Schubladenfront
    addBoxW2(589.5, cy1, 590.3, cy2, 0.38, 0xc8a97e, 0.07);
    const midY = (cy1 + cy2) / 2;
    addBoxW2(590.3, midY - 2.5, 590.9, midY + 2.5, 0.025, 0x64748b, 0.28);
    // Hohe weiße Schranktür darüber
    addBoxW2(589.5, cy1, 590.3, cy2, 1.54, 0xf8fafc, 0.47);
    // Vertikaler Türgriff
    const hy = (idx === 0) ? (cy2 - 1.5) : (cy1 + 1.5);
    addBoxW2(590.3, hy - 0.35, 591.0, hy + 0.35, 0.24, 0x64748b, 0.95);
  });
  // Spiegel auf der mittleren Tür des Flurschranks
  const flurMirror = new THREE.Mesh(
    new THREE.BoxGeometry(0.015, 1.32, (415.5 - 402.5) * SCALE),
    new THREE.MeshStandardMaterial({ color: 0xbae6fd, roughness: 0.08, metalness: 0.85 })
  );
  flurMirror.position.set(toX(590.4), 0.47 + 0.77, toZ(409));
  groups.w2.details.add(flurMirror);

  // ---- ESSZIMMER MÖBEL (px: 538..686, py: 445..582) ----
  // 1) Kleine Kommode / Schrank oben links in der Ecke (kompakt: px = 575.5..591.5, py = 453..461.5)
  addBoxW2(575.5, 453, 591.5, 461.5, 0.90, 0xc8a97e, 0.0);        // Korpus (Eiche)
  addBoxW2(575.3, 453, 591.8, 461.8, 0.04, 0xd6b88e, 0.90);       // Deckplatte (0.94 m)
  addBoxW2(576.5, 461.5, 583.2, 462.1, 0.78, 0xf8fafc, 0.06);     // Linke weiße Tür
  addBoxW2(583.8, 461.5, 590.5, 462.1, 0.78, 0xf8fafc, 0.06);     // Rechte weiße Tür
  addBoxW2(582.0, 462.1, 582.6, 462.6, 0.15, 0x64748b, 0.48);     // Griff links
  addBoxW2(584.4, 462.1, 585.0, 462.6, 0.15, 0x64748b, 0.48);     // Griff rechts

  // 2) Lange Kommode / Sideboard rechts an der Wand zum Wohnzimmer (Gelb markiert: px = 666..681, py = 455..530)
  addBoxW2(666, 455, 681, 530, 0.84, 0xc8a97e, 0.0);          // Sideboard-Korpus
  addBoxW2(665.5, 454.5, 681, 530.5, 0.04, 0xd6b88e, 0.84);   // Deckplatte (0.88 m)
  // 3 Schrankfächer/Schubladen-Spalten an der Vorderseite (Richtung Esszimmer, px = 666)
  [[457, 480], [481.5, 503.5], [505, 528]].forEach(([cy1, cy2]) => {
    const midY = (cy1 + cy2) / 2;
    addBoxW2(665.2, cy1, 666, cy2, 0.48, 0xf8fafc, 0.06);     // Untere weiße Tür
    addBoxW2(665.2, cy1, 666, cy2, 0.22, 0xf1f5f9, 0.58);     // Obere weiße Schublade
    addBoxW2(664.5, midY - 2.5, 665.2, midY + 2.5, 0.025, 0x64748b, 0.69); // Schubladengriff
  });

  // 3) Esstisch mit 4 Stühlen (Rot markiert: Tisch px = 562..618, py = 524..554 + je 2 Stühle oben und unten)
  addBoxW2(562, 524, 618, 554, 0.06, 0xb88655, 0.70);         // Esstisch-Platte (Holz, Höhe 0.76 m)
  [[564, 526], [613, 526], [564, 549], [613, 549]].forEach(([x, y]) =>
    addBoxW2(x, y, x + 3, y + 3, 0.70, 0x5c3a21)              // 4 Tischbeine
  );

  // 2 Stühle oben (nördlich vom Tisch, Blick nach Süden)
  [570, 596].forEach(cx => {
    addBoxW2(cx, 509, cx + 14, 521, 0.05, 0x475569, 0.44);    // Sitzfläche
    addBoxW2(cx, 507, cx + 14, 509.5, 0.46, 0x334155, 0.44);  // Rückenlehne (Nordseite)
    [[cx, 509], [cx + 11.5, 509], [cx, 518.5], [cx + 11.5, 518.5]].forEach(([lx, ly]) =>
      addBoxW2(lx, ly, lx + 2.5, ly + 2.5, 0.44, 0x1e293b)    // 4 Stuhlbeine
    );
  });

  // 2 Stühle unten (südlich vom Tisch, Blick nach Norden)
  [570, 596].forEach(cx => {
    addBoxW2(cx, 557, cx + 14, 569, 0.05, 0x475569, 0.44);    // Sitzfläche
    addBoxW2(cx, 568.5, cx + 14, 571, 0.46, 0x334155, 0.44);  // Rückenlehne (Südseite)
    [[cx, 557], [cx + 11.5, 557], [cx, 566.5], [cx + 11.5, 566.5]].forEach(([lx, ly]) =>
      addBoxW2(lx, ly, lx + 2.5, ly + 2.5, 0.44, 0x1e293b)    // 4 Stuhlbeine
    );
  });

  // ---- SCHLAFZIMMER MÖBEL (px: 546..629, py: 590..696) ----
  // 1) Großer Kleiderschrank oben an der Nordwand (Grün markiert: px = 546..608, py = 590..607)
  addBoxW2(546, 590, 608, 606.5, 0.06, 0xb8976c, 0.0);        // Holz-Sockelleiste unten
  addBoxW2(546, 590, 608, 606.5, 2.01, 0xf1f5f9, 0.06);       // Weißer Schrank-Korpus
  addBoxW2(545.8, 590, 608.2, 607.2, 0.03, 0xb8976c, 2.07);   // Holz-Abschlusskante oben (Höhe = 2.10 m)
  // 3 Schranktüren an der Südseite (py = 606.5..607.3)
  [[547, 566.5], [567.3, 586.7], [587.5, 607]].forEach(([cx1, cx2], idx) => {
    addBoxW2(cx1, 606.5, cx2, 607.3, 1.98, 0xf8fafc, 0.08);   // Weiße Schranktür
    const hx = (idx === 0) ? (cx2 - 1.5) : (cx1 + 1.5);
    addBoxW2(hx - 0.35, 607.3, hx + 0.35, 608.0, 0.28, 0x64748b, 0.95); // Vertikaler Griff
  });

  // 2) Doppelbett unten an der Südwand (Blau markiert: px = 572..618, py = 638..696)
  addBoxW2(572, 638, 618, 696, 0.48, 0x3f2a14, 0.0);          // Bettrahmen (Dunkelbraunes Holz)
  addBoxW2(572, 691, 618, 696, 1.05, 0x2b1b0c, 0.0);          // Kopfteil (an der Südwand py = 696)
  addBoxW2(575, 641, 615, 691, 0.18, 0xffffff, 0.48);         // Matratze (Weiß)
  addBoxW2(577, 676, 593.5, 689, 0.14, 0xe2e8f0, 0.66);       // Kissen links
  addBoxW2(596.5, 676, 613, 689, 0.14, 0xe2e8f0, 0.66);       // Kissen rechts
  addBoxW2(575, 641, 615, 674, 0.12, 0x1d4ed8, 0.66);         // Blaue Bettdecke

  // 3) Deckenlampe & echtes Raumlicht im Schlafzimmer (px = 588, py = 648)
  const schlafLampX = toX(588);
  const schlafLampZ = toZ(648);
  const schlafLampMount = new THREE.Mesh(
    new THREE.CylinderGeometry(0.26, 0.28, 0.05, 32),
    new THREE.MeshStandardMaterial({ color: 0x1e293b, roughness: 0.4, metalness: 0.5 })
  );
  schlafLampMount.position.set(schlafLampX, 2.52, schlafLampZ);
  groups.w2.details.add(schlafLampMount);

  const schlafBulbMat = new THREE.MeshStandardMaterial({
    color: 0xfef08a,
    emissive: 0xfacc15,
    emissiveIntensity: 1.35,
    roughness: 0.2
  });
  const schlafBulb = new THREE.Mesh(
    new THREE.CylinderGeometry(0.23, 0.23, 0.03, 32),
    schlafBulbMat
  );
  schlafBulb.position.set(schlafLampX, 2.49, schlafLampZ);
  groups.w2.details.add(schlafBulb);

  const schlafConeMat = new THREE.MeshBasicMaterial({
    color: 0xfef08a,
    transparent: true,
    opacity: 0.11,
    side: THREE.DoubleSide,
    depthWrite: false
  });
  const schlafConeMesh = new THREE.Mesh(
    new THREE.ConeGeometry(2.6, 2.46, 36, 1, true),
    schlafConeMat
  );
  schlafConeMesh.position.set(schlafLampX, 1.25, schlafLampZ);
  scene.add(schlafConeMesh);

  const schlafFloorGlowCanvas = document.createElement('canvas');
  schlafFloorGlowCanvas.width = 256;
  schlafFloorGlowCanvas.height = 256;
  const sfgCtx = schlafFloorGlowCanvas.getContext('2d');
  const sfgGrad = sfgCtx.createRadialGradient(128, 128, 8, 128, 128, 126);
  sfgGrad.addColorStop(0, 'rgba(254, 240, 138, 0.50)');
  sfgGrad.addColorStop(0.55, 'rgba(250, 204, 21, 0.22)');
  sfgGrad.addColorStop(1, 'rgba(250, 204, 21, 0.0)');
  sfgCtx.fillStyle = sfgGrad;
  sfgCtx.fillRect(0, 0, 256, 256);
  const schlafFloorGlowTex = new THREE.CanvasTexture(schlafFloorGlowCanvas);
  const schlafFloorGlowMesh = new THREE.Mesh(
    new THREE.PlaneGeometry(4.2, 5.2),
    new THREE.MeshBasicMaterial({
      map: schlafFloorGlowTex,
      transparent: true,
      depthWrite: false
    })
  );
  schlafFloorGlowMesh.rotation.x = -Math.PI / 2;
  schlafFloorGlowMesh.position.set(schlafLampX, 0.032, schlafLampZ);
  scene.add(schlafFloorGlowMesh);

  const schlafPointLight = new THREE.PointLight(0xffe49e, 1.6, 9.5, 1.35);
  schlafPointLight.position.set(schlafLampX, 2.38, schlafLampZ);
  scene.add(schlafPointLight);

  window.schlafRoomLight = {
    isOn: true,
    roomId: 'label-schlaf',
    worldPos: new THREE.Vector3(schlafLampX, 2.15, schlafLampZ),
    bulbMat: schlafBulbMat,
    coneMesh: schlafConeMesh,
    floorGlowMesh: schlafFloorGlowMesh,
    pointLight: schlafPointLight,
    meshes: [schlafBulb, schlafConeMesh, schlafFloorGlowMesh]
  };

  // 4) Umlaufendes RGB Decken-LED-Band oben an der Decke entlang im Schlafzimmer (px = 546..629, py = 590..696)
  // Hochwertige architektonische Lichtvoute: Bündige Alu-Profile, Diffusor-LEDs und weicher Wand-Wash,
  // die sich dynamisch an das Cutaway anpassen (keine schwebenden Wände/Käfige im leeren Raum!).
  const schlafLedStripMat = new THREE.MeshStandardMaterial({
    color: 0x38bdf8,
    emissive: 0x38bdf8,
    emissiveIntensity: 2.0,
    roughness: 0.15
  });
  const schlafLedRailMat = new THREE.MeshStandardMaterial({
    color: 0x334155,
    roughness: 0.5,
    metalness: 0.3
  });

  const cLx1 = 546.0, cLx2 = 629.0, cLy1 = 590.0, cLy2 = 696.0;
  const cMidX = (cLx1 + cLx2) / 2;
  const cMidY = (cLy1 + cLy2) / 2;
  const cSpanW = (cLx2 - cLx1) * SCALE;
  const cSpanD = (cLy2 - cLy1) * SCALE;
  const cLedY = 2.68; // Bündig knapp unter der Deckenoberkante (H_WALL = 2.75)

  // Weicher Wand-Verlauf nach unten (Wand-Grazing statt schwebender Raum-Ebenen)
  const schlafWashCanvas = document.createElement('canvas');
  schlafWashCanvas.width = 64;
  schlafWashCanvas.height = 256;
  const swCtx = schlafWashCanvas.getContext('2d');
  const swGrad = swCtx.createLinearGradient(0, 0, 0, 256);
  swGrad.addColorStop(0.0, 'rgba(255, 255, 255, 0.70)');
  swGrad.addColorStop(0.18, 'rgba(255, 255, 255, 0.40)');
  swGrad.addColorStop(0.55, 'rgba(255, 255, 255, 0.12)');
  swGrad.addColorStop(1.0, 'rgba(255, 255, 255, 0.0)');
  swCtx.fillStyle = swGrad;
  swCtx.fillRect(0, 0, 64, 256);
  const schlafWashTex = new THREE.CanvasTexture(schlafWashCanvas);

  const schlafWashMat = new THREE.MeshBasicMaterial({
    color: 0x38bdf8,
    map: schlafWashTex,
    transparent: true,
    opacity: 0.55,
    side: THREE.DoubleSide,
    depthWrite: false
  });

  const schlafLedGroup = new THREE.Group();
  scene.add(schlafLedGroup);

  const washH = 0.42;
  const washY = cLedY - washH / 2;

  // 1. Nordwand (py = 590, Blick nach +Z)
  const railNorth = new THREE.Mesh(new THREE.BoxGeometry(cSpanW, 0.028, 0.022), schlafLedRailMat);
  railNorth.position.set(toX(cMidX), cLedY, toZ(cLy1) + 0.011);
  schlafLedGroup.add(railNorth);

  const cStripNorth = new THREE.Mesh(new THREE.BoxGeometry(cSpanW, 0.016, 0.014), schlafLedStripMat);
  cStripNorth.position.set(toX(cMidX), cLedY - 0.003, toZ(cLy1) + 0.022);
  schlafLedGroup.add(cStripNorth);

  const washNorth = new THREE.Mesh(new THREE.PlaneGeometry(cSpanW, washH), schlafWashMat);
  washNorth.position.set(toX(cMidX), washY, toZ(cLy1) + 0.006);
  schlafLedGroup.add(washNorth);

  // 2. Südwand (py = 696, Blick nach -Z)
  const railSouth = new THREE.Mesh(new THREE.BoxGeometry(cSpanW, 0.028, 0.022), schlafLedRailMat);
  railSouth.position.set(toX(cMidX), cLedY, toZ(cLy2) - 0.011);
  schlafLedGroup.add(railSouth);

  const cStripSouth = new THREE.Mesh(new THREE.BoxGeometry(cSpanW, 0.016, 0.014), schlafLedStripMat);
  cStripSouth.position.set(toX(cMidX), cLedY - 0.003, toZ(cLy2) - 0.022);
  schlafLedGroup.add(cStripSouth);

  const washSouth = new THREE.Mesh(new THREE.PlaneGeometry(cSpanW, washH), schlafWashMat);
  washSouth.rotation.y = Math.PI;
  washSouth.position.set(toX(cMidX), washY, toZ(cLy2) - 0.006);
  schlafLedGroup.add(washSouth);

  // 3. Westwand (px = 546, Blick nach +X)
  const railWest = new THREE.Mesh(new THREE.BoxGeometry(0.022, 0.028, cSpanD), schlafLedRailMat);
  railWest.position.set(toX(cLx1) + 0.011, cLedY, toZ(cMidY));
  schlafLedGroup.add(railWest);

  const cStripWest = new THREE.Mesh(new THREE.BoxGeometry(0.014, 0.016, cSpanD), schlafLedStripMat);
  cStripWest.position.set(toX(cLx1) + 0.022, cLedY - 0.003, toZ(cMidY));
  schlafLedGroup.add(cStripWest);

  const washWest = new THREE.Mesh(new THREE.PlaneGeometry(cSpanD, washH), schlafWashMat);
  washWest.rotation.y = Math.PI / 2;
  washWest.position.set(toX(cLx1) + 0.006, washY, toZ(cMidY));
  schlafLedGroup.add(washWest);

  // 4. Ostwand (px = 629, Blick nach -X)
  const railEast = new THREE.Mesh(new THREE.BoxGeometry(0.022, 0.028, cSpanD), schlafLedRailMat);
  railEast.position.set(toX(cLx2) - 0.011, cLedY, toZ(cMidY));
  schlafLedGroup.add(railEast);

  const cStripEast = new THREE.Mesh(new THREE.BoxGeometry(0.014, 0.016, cSpanD), schlafLedStripMat);
  cStripEast.position.set(toX(cLx2) - 0.022, cLedY - 0.003, toZ(cMidY));
  schlafLedGroup.add(cStripEast);

  const washEast = new THREE.Mesh(new THREE.PlaneGeometry(cSpanD, washH), schlafWashMat);
  washEast.rotation.y = -Math.PI / 2;
  washEast.position.set(toX(cLx2) - 0.006, washY, toZ(cMidY));
  schlafLedGroup.add(washEast);

  const schlafLedSegments = [
    { side: 'N', stripMesh: cStripNorth, railMesh: railNorth, washMesh: washNorth },
    { side: 'S', stripMesh: cStripSouth, railMesh: railSouth, washMesh: washSouth },
    { side: 'W', stripMesh: cStripWest, railMesh: railWest, washMesh: washWest },
    { side: 'E', stripMesh: cStripEast, railMesh: railEast, washMesh: washEast }
  ];

  // Sanfter radialer Farb-Schimmer auf dem Schlafzimmerboden
  const schlafLedFloorCanvas = document.createElement('canvas');
  schlafLedFloorCanvas.width = 256;
  schlafLedFloorCanvas.height = 256;
  const slfCtx = schlafLedFloorCanvas.getContext('2d');
  const slfGrad = slfCtx.createRadialGradient(128, 128, 8, 128, 128, 126);
  slfGrad.addColorStop(0.0, 'rgba(255, 255, 255, 0.42)');
  slfGrad.addColorStop(0.55, 'rgba(255, 255, 255, 0.14)');
  slfGrad.addColorStop(1.0, 'rgba(255, 255, 255, 0.0)');
  slfCtx.fillStyle = slfGrad;
  slfCtx.fillRect(0, 0, 256, 256);
  const schlafLedFloorTex = new THREE.CanvasTexture(schlafLedFloorCanvas);

  const schlafLedFloorGlowMat = new THREE.MeshBasicMaterial({
    color: 0x38bdf8,
    map: schlafLedFloorTex,
    transparent: true,
    opacity: 0.28,
    depthWrite: false
  });
  const schlafLedFloorGlowMesh = new THREE.Mesh(
    new THREE.PlaneGeometry(cSpanW * 0.90, cSpanD * 0.90),
    schlafLedFloorGlowMat
  );
  schlafLedFloorGlowMesh.rotation.x = -Math.PI / 2;
  schlafLedFloorGlowMesh.position.set(toX(cMidX), 0.030, toZ(cMidY));
  scene.add(schlafLedFloorGlowMesh);

  const schlafLedPointLight = new THREE.PointLight(0x38bdf8, 1.6, 9.0, 1.4);
  schlafLedPointLight.position.set(toX(cMidX), 2.50, toZ(cMidY));
  scene.add(schlafLedPointLight);

  window.schlafCeilingLed = {
    isOn: true,
    colorHex: '#38bdf8',
    colorName: 'Cyan',
    brightness: 100,
    roomId: 'label-schlaf',
    worldPos: new THREE.Vector3(toX(578), 2.50, toZ(596)),
    stripMat: schlafLedStripMat,
    railMat: schlafLedRailMat,
    washMat: schlafWashMat,
    floorGlowMesh: schlafLedFloorGlowMesh,
    floorGlowMat: schlafLedFloorGlowMat,
    pointLight: schlafLedPointLight,
    segments: schlafLedSegments,
    meshes: [cStripNorth, cStripSouth, cStripWest, cStripEast, railNorth, railSouth, railWest, railEast]
  };

  // ---- MELANIERS ZIMMER MÖBEL (px: 753..885, py: 590..696) ----
  // 1) Langer, großer Klamottenschrank oben an der Nordwand (Rot markiert: px = 782..876, py = 590..609)
  addBoxW2(782, 590, 876, 608.5, 0.06, 0xb8976c, 0.0);        // Holz-Sockelleiste unten
  addBoxW2(782, 590, 876, 608.5, 2.01, 0xf1f5f9, 0.06);       // Weißer Schrank-Korpus
  addBoxW2(781.8, 590, 876.2, 609.2, 0.03, 0xb8976c, 2.07);   // Holz-Abschlusskante oben (Höhe = 2.10 m)
  // 4 breite Schranktüren an der Südseite (py = 608.5..609.3)
  [[783, 805.2], [806, 828.2], [829, 851.2], [852, 875]].forEach(([cx1, cx2], idx) => {
    addBoxW2(cx1, 608.5, cx2, 609.3, 1.98, 0xf8fafc, 0.08);   // Weiße Schranktür
    const hx = (idx % 2 === 0) ? (cx2 - 1.5) : (cx1 + 1.5);
    addBoxW2(hx - 0.35, 609.3, hx + 0.35, 610.0, 0.28, 0x64748b, 0.95); // Vertikaler Griff
  });

  // 2) Bett unten rechts (Grün markiert: px = 842..884, py = 636..696)
  addBoxW2(842, 636, 884, 696, 0.48, 0x3f2a14, 0.0);          // Bettrahmen
  addBoxW2(842, 691, 884, 696, 1.02, 0x2b1b0c, 0.0);          // Kopfteil (an der Südwand py = 696)
  addBoxW2(845, 639, 881, 691, 0.18, 0xffffff, 0.48);         // Matratze (Weiß)
  addBoxW2(849, 676, 877, 689, 0.14, 0xe2e8f0, 0.66);         // Kissen
  addBoxW2(845, 639, 881, 674, 0.12, 0x16a34a, 0.66);         // Grüne Bettdecke

  // 3) Eck-Schreibtisch unten links (Lila markiert: L-Form an West- & Südwand, px = 753..790, py = 638..696)
  addBoxW2(753, 638, 773, 696, 0.05, 0xf8fafc, 0.70);         // Tischplatte Westschenkel (Höhe 0.75 m)
  addBoxW2(773, 676, 790, 696, 0.05, 0xf8fafc, 0.70);         // Tischplatte Südschenkel (Ecke)
  [[754, 639], [769, 639], [754, 692], [786, 677], [786, 692]].forEach(([x, y]) =>
    addBoxW2(x, y, x + 2.8, y + 2.8, 0.70, 0x64748b)          // Tischbeine
  );
  // Schreibtischstuhl vor dem Schreibtisch
  addBoxW2(776, 652, 788, 666, 0.05, 0x475569, 0.44);         // Sitzfläche
  addBoxW2(786, 652, 788.5, 666, 0.46, 0x334155, 0.44);       // Rückenlehne
  [[776, 652], [785.5, 652], [776, 663.5], [785.5, 663.5]].forEach(([lx, ly]) =>
    addBoxW2(lx, ly, lx + 2.5, ly + 2.5, 0.44, 0x1e293b)      // Stuhlbeine
  );

  // 4) Fernseher an der linken Wand über dem Schreibtisch (Gelb markiert: px = 753, py = 656..684)
  addBoxW2(753, 656, 754.5, 684, 0.60, 0x0f172a, 1.12);       // TV-Gehäuse an der Westwand
  const melanieTvMesh = addBoxW2(754.2, 657.2, 754.8, 682.8, 0.54, 0x1e293b, 1.15); // TV-Display
  registerScreen({
    id: 'tv-melanie',
    name: 'Fernseher (Melaniers Zimmer)',
    shortLabel: 'Fernseher',
    kind: 'tv',
    roomId: 'label-melanie',
    apt: 'w2',
    mesh: melanieTvMesh,
    x1: 754.2, y1: 657.2, x2: 754.8, y2: 682.8, h: 0.54, yOff: 1.15,
    faceDir: 'E',
    isOn: false
  });

  // 5) Regal unten an der Südwand rechts neben dem Schreibtisch (Orange markiert: px = 791..812, py = 681..696)
  addBoxW2(791, 681, 812, 696, 1.45, 0xc8a97e, 0.0);          // Regal-Korpus (Holzoptik)
  for (let s = 0; s < 4; s++) {
    addBoxW2(791.8, 680.5, 811.2, 695.5, 0.035, 0xf8fafc, 0.32 + s * 0.34); // 4 Regalböden
  }

  // ---- WOHNZIMMER MÖBEL (px: 691..885, py: 453..574) ----
  // 1) Schwarzes U-förmiges Sofa / Wohnlandschaft (Grün markiert: oben/links px = 691..816, py = 453..530)
  // Linker Schenkel an der Westwand (px = 691..721, py = 453..530)
  addBoxW2(691, 453, 721, 530, 0.30, 0x18181b, 0.0);          // Sofa-Basis links (Schwarz)
  addBoxW2(697, 459, 720, 526, 0.14, 0x27272a, 0.30);         // Sitzpolster links (Anthrazit-Schwarz)
  addBoxW2(691, 453, 697, 530, 0.78, 0x18181b, 0.0);          // Rückenlehne Westwand
  addBoxW2(691, 526, 721, 530, 0.56, 0x18181b, 0.0);          // Armlehne vorne links
  // Mittlerer Hauptteil an der Nordwand (px = 721..784, py = 453..483)
  addBoxW2(721, 453, 784, 483, 0.30, 0x18181b, 0.0);          // Sofa-Basis Mitte (Schwarz)
  addBoxW2(721, 459, 752, 482, 0.14, 0x27272a, 0.30);         // Sitzpolster Mitte-links
  addBoxW2(753, 459, 784, 482, 0.14, 0x27272a, 0.30);         // Sitzpolster Mitte-rechts
  addBoxW2(691, 453, 816, 459, 0.78, 0x18181b, 0.0);          // Durchgehende Rückenlehne Nordwand
  // Rechter Schenkel / Ottomane (px = 784..816, py = 453..514)
  addBoxW2(784, 453, 816, 514, 0.30, 0x18181b, 0.0);          // Sofa-Basis rechts (Schwarz)
  addBoxW2(785, 459, 811, 513, 0.14, 0x27272a, 0.30);         // Liegepolster rechts (Ottomane)
  addBoxW2(811, 453, 816, 495, 0.58, 0x18181b, 0.0);          // Armlehne rechts außen

  // 2) Mittiger Glastisch / Couchtisch vor dem Sofa (Orange markiert: px = 735..770, py = 500..526)
  // Schwarzes Gestell + untere Ablage + transparente Glasplatte oben (Höhe = 0.42 m)
  [[736, 501], [767, 501], [736, 523], [767, 523]].forEach(([lx, ly]) =>
    addBoxW2(lx, ly, lx + 2.2, ly + 2.2, 0.40, 0x18181b, 0.0) // 4 schwarze Tischbeine
  );
  addBoxW2(738, 503, 767, 523, 0.02, 0x27272a, 0.14);         // Untere dunkle Ablageplatte
  const glassTableTop = new THREE.Mesh(
    new THREE.BoxGeometry((770 - 735) * SCALE, 0.03, (526 - 500) * SCALE),
    new THREE.MeshStandardMaterial({
      color: 0xbae6fd,
      transparent: true,
      opacity: 0.55,
      roughness: 0.08,
      metalness: 0.25
    })
  );
  glassTableTop.position.set(toX(752.5), 0.415, toZ(513));
  groups.w2.details.add(glassTableTop);

  // 3) Schwebende schwarze TV-Kommode + großer Wand-Fernseher darüber (Blau markiert: an der Südwand py = 574, px = 724..796)
  // Schwebendes schwarzes Lowboard (schwebt auf yOff = 0.22 m..0.56 m)
  addBoxW2(724, 561, 796, 574, 0.34, 0x11181b, 0.22);         // Schwarzer schwebender Korpus
  addBoxW2(723.5, 560.5, 796.5, 574, 0.03, 0x1e293b, 0.56);   // Schwarze Deckplatte (yOff = 0.59 m)
  // 3 schwarze Klappen/Schubladen-Fronten mit dezenten Griffleisten
  [[725.5, 747.5], [749, 771], [772.5, 794.5]].forEach(([fx1, fx2]) => {
    const midX = (fx1 + fx2) / 2;
    addBoxW2(fx1, 560.2, fx2, 561, 0.28, 0x18181b, 0.25);     // Schwarze Frontklappe
    addBoxW2(midX - 4, 559.6, midX + 4, 560.2, 0.02, 0x64748b, 0.49); // Griffleiste
  });

  // Großer Wand-Fernseher direkt über der schwebenden Kommode an der Südwand (px = 734..786, py = 572..574)
  addBoxW2(734, 572, 786, 574, 0.88, 0x090d16, 0.82);         // Großer schwarzer TV-Rahmen an der Wand
  const wohnzimmerTvMesh = addBoxW2(735.5, 571.5, 784.5, 572.3, 0.82, 0x111827, 0.85); // Großes TV-Display
  registerScreen({
    id: 'tv-wohnzimmer',
    name: 'Fernseher (Wohnzimmer)',
    shortLabel: 'Fernseher',
    kind: 'tv',
    roomId: 'label-wohnzimmer',
    apt: 'w2',
    mesh: wohnzimmerTvMesh,
    x1: 735.5, y1: 571.5, x2: 784.5, y2: 572.3, h: 0.82, yOff: 0.85,
    faceDir: 'N',
    isOn: true
  });

  // ---- WOHNUNG 2: GÄSTE-WC (LINKS: px = 639..669) & BADEZIMMER (RECHTS: px = 679..743) ----
  // 1) Klo / WC im linken Raum unten (Blau markiert: an der Südwand py = 696, px = 648..661)
  addBoxW2(648, 689, 661, 696, 0.90, 0xe2e8f0, 0.0);          // Vorwand-Spülkasten
  addBoxW2(647.8, 688.8, 661.2, 696, 0.03, 0xf8fafc, 0.90);   // Ablage auf dem Spülkasten
  addBoxW2(652, 688.4, 657, 689.1, 0.14, 0x94a3b8, 0.66);     // Silberne Spültaste
  addBoxW2(650, 675, 659, 689, 0.38, 0xffffff, 0.04);         // Weiße Keramik-WC-Schüssel
  addBoxW2(649.5, 674.5, 659.5, 688.5, 0.04, 0xf8fafc, 0.42); // WC-Brille / Klodeckel

  // 2) Waschbecken im linken Raum rechts an der Wand (Rot markiert: an der Ostwand px = 669, py = 648..666)
  addBoxW2(657, 648, 669, 666, 0.74, 0xc8a97e, 0.06);         // Waschtisch-Unterschrank
  addBoxW2(656.5, 647.5, 669, 666.5, 0.08, 0xffffff, 0.80);   // Weißes Keramik-Waschbecken
  addBoxW2(658.5, 650, 666.5, 664, 0.03, 0xe2e8f0, 0.865);    // Waschbecken-Mulde innen
  addBoxW2(666.8, 656.2, 668.4, 657.8, 0.14, 0x94a3b8, 0.88); // Silberner Wasserhahn-Fuß
  addBoxW2(664.2, 656.4, 668.2, 657.6, 0.025, 0x94a3b8, 1.00); // Wasserhahn-Auslauf

  // 3) Waschmaschine im rechten Bad oben rechts (Grün markiert: px = 721..742, py = 628..649)
  addBoxW2(721, 628, 742, 649, 0.82, 0xf8fafc, 0.0);          // Waschmaschinen-Gehäuse (Weiß)
  addBoxW2(720.6, 627.6, 742.4, 649.4, 0.03, 0xe2e8f0, 0.82); // Deckplatte oben (0.85 m)
  addBoxW2(720.2, 628.5, 721, 648.5, 0.14, 0xcbd5e1, 0.67);   // Bedienblende vorne (Westseite)
  addBoxW2(719.8, 642, 720.4, 647.5, 0.08, 0xf1f5f9, 0.70);   // Waschmittelschublade
  addBoxW2(719.7, 637, 720.4, 639.2, 0.06, 0x475569, 0.71);   // Programm-Drehregler
  addBoxW2(719.8, 630, 720.4, 635, 0.05, 0x0f172a, 0.715);    // Schwarzes LED-Display
  // Rundes Bullauge (Waschtrommel-Tür an der Westseite px = 720.5, py = 638.5)
  const w2DrumRing = new THREE.Mesh(
    new THREE.CylinderGeometry(0.24, 0.24, 0.05, 24),
    new THREE.MeshStandardMaterial({ color: 0x64748b, roughness: 0.3, metalness: 0.6 })
  );
  w2DrumRing.rotation.z = Math.PI / 2;
  w2DrumRing.position.set(toX(720.5), 0.41, toZ(638.5));
  groups.w2.details.add(w2DrumRing);

  const w2DrumGlass = new THREE.Mesh(
    new THREE.CylinderGeometry(0.18, 0.18, 0.06, 24),
    new THREE.MeshStandardMaterial({ color: 0x0f172a, roughness: 0.15, metalness: 0.8 })
  );
  w2DrumGlass.rotation.z = Math.PI / 2;
  w2DrumGlass.position.set(toX(720.4), 0.41, toZ(638.5));
  groups.w2.details.add(w2DrumGlass);

  // 4) Badewanne im rechten Bad unten rechts (Grau markiert: px = 716..743, py = 652..696)
  addBoxW2(716, 652, 743, 696, 0.52, 0xcbd5e1, 0.0);          // Graue Badewannen-Verkleidung (Schürze)
  // Weiße Badewannen-Ränder (4 Seiten) auf Höhe 0.52..0.56 m
  addBoxW2(715.6, 651.6, 743, 655.5, 0.04, 0xffffff, 0.52);   // Wannenrand Nord
  addBoxW2(715.6, 692, 743, 696, 0.04, 0xffffff, 0.52);       // Wannenrand Süd
  addBoxW2(715.6, 655.5, 719.5, 692, 0.04, 0xffffff, 0.52);   // Wannenrand West
  addBoxW2(739.5, 655.5, 743, 692, 0.04, 0xffffff, 0.52);     // Wannenrand Ost
  // Wannen-Innenwände & leicht bläulicher Wannenboden innen
  addBoxW2(719.5, 655.5, 739.5, 692, 0.14, 0xf8fafc, 0.0);    // Weißer Wannenboden
  addBoxW2(720.5, 656.5, 738.5, 691, 0.02, 0xe0f2fe, 0.14);   // Innere Liegefläche
  addBoxW2(728, 658, 731, 660.5, 0.025, 0x64748b, 0.15);      // Silberner Abfluss
  // Silberne Badewannen-Armatur & Wasserhahn an der Ostwand (px = 741, py = 673.5)
  addBoxW2(739.5, 671, 742.5, 676.5, 0.06, 0x94a3b8, 0.58);   // Wannen-Mischbatterie
  addBoxW2(736.5, 673, 740, 674.5, 0.03, 0x94a3b8, 0.60);     // Hahn-Auslauf in die Wanne
})();

// ============================================================================
// INTERACTIVE UI CONTROLS
// ============================================================================
let currentFilter = 'all';
let is2DView = false;
let wallHeightState = 2; // 2 = Voll (1.0), 1 = Halb (0.42), 0 = Flach (0.06)
let targetWallScaleY = 1.0;

let camAnim = null;
let activeRoomId = null;
let activeDoorIndex = 0;

function animateCameraTo(targetPos, targetLookAt, duration = 750, targetFov = 40) {
  camAnim = {
    startPos: camera.position.clone(),
    endPos: targetPos.clone(),
    startTarget: controls.target.clone(),
    endTarget: targetLookAt.clone(),
    startFov: camera.fov,
    endFov: targetFov,
    startTime: performance.now(),
    duration
  };
}

function applySectionOpacity(sectionKey, weight) {
  const m = mats[sectionKey];
  const isDimmed = weight < 0.5;

  m.wall.opacity = weight;
  m.wall.depthWrite = !isDimmed;
  m.wallCap.opacity = weight;
  m.wallCap.depthWrite = !isDimmed;
  m.railingMetal.opacity = weight;
  m.railingMetal.depthWrite = !isDimmed;
  m.floor.opacity = isDimmed ? 0.18 : 0.96;
  m.floor.depthWrite = !isDimmed;
  m.floorSub.opacity = isDimmed ? 0.18 : 0.96;
  m.floorSub.depthWrite = !isDimmed;
  m.durchgangFloor.opacity = isDimmed ? 0.22 : 0.96;
  m.durchgangFloor.depthWrite = !isDimmed;
  m.badNeuFloor.opacity = isDimmed ? 0.20 : 0.96;
  m.badNeuFloor.depthWrite = !isDimmed;
  m.doorLeaf.opacity = weight;
  m.glass.opacity = isDimmed ? 0.08 : 0.36;
  m.arcLine.opacity = isDimmed ? 0.15 : 0.85;
}

function refreshAllSectionMaterials() {
  if (currentFilter === 'all') {
    applySectionOpacity('w1', 1.0);
    applySectionOpacity('w2', 1.0);
    applySectionOpacity('common', 1.0);
  } else if (currentFilter === 'w1') {
    applySectionOpacity('w1', 1.0);
    applySectionOpacity('w2', 0.14);
    applySectionOpacity('common', 0.38);
  } else if (currentFilter === 'w2') {
    applySectionOpacity('w1', 0.14);
    applySectionOpacity('w2', 1.0);
    applySectionOpacity('common', 0.38);
  }
}

// ============================================================================
// ROOM DEFINITIONS WITH CUTAWAY & TOP-DOWN ISOLATION PRESETS
// ============================================================================
const roomLabels = [
  // ---- WOHNUNG 1 ----
  {
    id: 'label-simon',
    apt: 'w1',
    text: 'Simons Zimmer',
    color: '#ef4444',
    x: 725,
    z: 180,
    bounds: [638, 110, 812, 248],
    // Kamera von Süd-West -> Süd- & Westwand unsichtbar, Nord- & Ostwand (mit beiden Rollläden!) sichtbar
    cutawayDir: [-1, 1]
  },
  {
    id: 'label-arbeit',
    apt: 'w1',
    text: 'Arbeitszimmer',
    color: '#92400e',
    x: 852,
    z: 276,
    bounds: [812, 221, 893, 330],
    // Kamera von Süd-Ost -> Nordwand (Rollladen!) & Westwand (Schreibtisch + Vorderseite vom Monitor!) sichtbar
    cutawayDir: [1, 1]
  },
  {
    id: 'label-abstell',
    apt: 'w1',
    text: 'Abstellkammer',
    color: '#f59e0b',
    x: 675,
    z: 270,
    bounds: [638, 248, 712, 292],
    cutawayDir: [1, 1]
  },
  {
    id: 'label-flur1',
    apt: 'w1',
    text: 'Flur (Oben)',
    color: '#2563eb',
    x: 762,
    z: 292,
    bounds: [712, 248, 812, 330],
    cutawayDir: [-1, 1]
  },
  {
    id: 'label-flur2',
    apt: 'w1',
    text: 'Flur (Vorne)',
    color: '#2563eb',
    x: 661,
    z: 335,
    bounds: [638, 292, 684, 445],
    // Kamera von Süd-West -> Nordecke (Telefon-Eckregal) & Ostwand (Jackenhaken) sichtbar
    cutawayDir: [-1, 1]
  },
  {
    id: 'label-bad',
    apt: 'w1',
    text: 'Neues Bad',
    color: '#fb923c',
    x: 722,
    z: 404,
    bounds: [684, 362, 760, 445],
    poly: [
      [684, 362],
      [714, 362],
      [714, 386],
      [760, 386],
      [760, 445],
      [684, 445]
    ],
    // Kamera von Nord-Ost -> Nord- & Ostwand unsichtbar, Westwand (Dusche) & Südwand (WC/Waschtisch) sichtbar
    cutawayDir: [1, -1]
  },
  {
    id: 'label-martin',
    apt: 'w1',
    text: 'Martins Zimmer',
    color: '#22c55e',
    x: 850,
    z: 388,
    bounds: [760, 330, 950.5, 445],
    poly: [
      [760, 330],
      [893, 330],
      [950.5, 387.5],
      [893, 445],
      [760, 445]
    ],
    // Kamera von Süd-Süd-Ost -> Westwand (Vorderseite PC-Monitor!), Nordwand (Vorderseite TV!) & Nord-Ost-Erker (Rollladen!) sichtbar
    cutawayDir: [0.65, 1]
  },

  // ---- WOHNUNG 2 ----
  {
    id: 'label-eingangsflur',
    apt: 'w2',
    text: 'Eingangsflur',
    color: '#10b981',
    x: 604,
    z: 408,
    bounds: [570, 372, 638, 445],
    // Kamera von Süd-Ost -> Westwand (Garderobenschrank) & Nordwand sichtbar
    cutawayDir: [1, 1]
  },
  {
    id: 'label-kueche',
    apt: 'w2',
    text: 'Küche',
    color: '#7e22ce',
    x: 528,
    z: 435,
    bounds: [486, 372, 570, 498],
    // Kamera von Süd-West -> Nord- & Ostwand (L-Küche, Kühlschrank, Küchentisch & Steckdose!) sichtbar
    cutawayDir: [-1, 1]
  },
  {
    id: 'label-esszimmer',
    apt: 'w2',
    text: 'Esszimmer',
    color: '#c8a27a',
    x: 612,
    z: 540,
    bounds: [538, 445, 686, 582],
    cutawayDir: [1, 1]
  },
  {
    id: 'label-wohnzimmer',
    apt: 'w2',
    text: 'Wohnzimmer',
    color: '#94a3b8',
    x: 781,
    z: 513,
    bounds: [686, 445, 893, 582],
    // Kamera von Nord-West -> Südwand (Vorderseite vom großen Wohnzimmer-TV!) & Ostwand (Balkonfenster/Tür) sichtbar
    cutawayDir: [-1, -1]
  },
  {
    id: 'label-schlaf',
    apt: 'w2',
    text: 'Schlafzimmer',
    color: '#5b3a29',
    x: 586,
    z: 645,
    bounds: [538, 582, 634, 704],
    // Kamera von Süd-Ost -> Nordwand (Kleiderschrank) & Westwand (Fenster mit Rollladen!) sichtbar
    cutawayDir: [1, 1]
  },
  {
    id: 'label-wc-w2',
    apt: 'w2',
    text: 'WC',
    color: '#3b82f6',
    x: 654,
    z: 664,
    bounds: [634, 622, 674, 704],
    // Kamera von Nord-West -> Südwand (WC/Fenster) & Ostwand (Waschbecken) sichtbar
    cutawayDir: [-1, -1]
  },
  {
    id: 'label-bad-w2',
    apt: 'w2',
    text: 'Bad',
    color: '#06b6d4',
    x: 711,
    z: 664,
    bounds: [674, 622, 748, 704],
    // Kamera von Nord-West -> Südwand (Fenster) & Ostwand (Waschmaschine & Badewanne) sichtbar
    cutawayDir: [-1, -1]
  },
  {
    id: 'label-melanie',
    apt: 'w2',
    text: 'Melaniers Zimmer',
    color: '#38bdf8',
    x: 820,
    z: 645,
    bounds: [748, 582, 893, 704],
    // Kamera von Süden -> Westwand (Vorderseite vom TV über dem Schreibtisch!), Nordwand (Schrank) & Ostwand (beide Rollläden!) gleichzeitig sichtbar
    cutawayDir: [0.08, 1.15],
    defaultHiddenSides: ['S']
  }
];

const GH_ROOM_SVG = `<svg class="gh-inline-icon" viewBox="0 0 24 24" fill="none"><path d="M3 10.5L12 3l9 7.5V20a1 1 0 0 1-1 1h-5.5v-6h-5v6H4a1 1 0 0 1-1-1v-9.5z" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/></svg>`;

// Nur Räume mit etwas zum Steuern/Anschauen (Rollladen, Licht, LED, Steckdose, Fernseher, Monitor)
// bekommen ein Label und lassen sich öffnen – alle anderen bleiben in der Übersicht ohne Label.
const CONTROL_ROOM_IDS = new Set([
  ...(window.shuttersRegistry || []).map(s => s.roomId),
  ...(window.screensRegistry || []).map(s => s.roomId),
  ...(window.simonRoomLight || window.simonDeskLed || window.simonPc ? ['label-simon'] : []),
  ...(window.schlafRoomLight || window.schlafCeilingLed ? ['label-schlaf'] : []),
  ...(window.kuecheSocket ? ['label-kueche'] : [])
].filter(Boolean));
window.isControlRoom = id => CONTROL_ROOM_IDS.has(id);

roomLabels.forEach(r => {
  if (!CONTROL_ROOM_IDS.has(r.id)) return;
  const el = document.createElement('div');
  el.className = 'apt-label room';
  el.id = r.id;
  el.title = `Klicken, um „${r.text}“ abgetrennt anzuschauen`;
  el.innerHTML = `${GH_ROOM_SVG}<span>${r.text}</span>`;
  el.addEventListener('click', e => {
    e.stopPropagation();
    enterRoomView(r.id, 'cutaway');
  });
  document.getElementById('labels-layer').appendChild(el);

  // Also populate the mobile-friendly Quick Room Picker Sheet
  const gridEl = document.getElementById(r.apt === 'w2' ? 'room-picker-w2' : 'room-picker-w1');
  if (gridEl) {
    const itemBtn = document.createElement('button');
    itemBtn.className = 'room-picker-item';
    itemBtn.innerHTML = `${GH_ROOM_SVG}<span>${r.text}</span>`;
    itemBtn.onclick = () => {
      toggleRoomPickerSheet(false);
      toggleRoomHeaderDropdown(null, false);
      enterRoomView(r.id, 'cutaway');
    };
    gridEl.appendChild(itemBtn);
  }
});

window.toggleRoomPickerSheet = function(forceState) {
  const sheet = document.getElementById('room-picker-sheet');
  const btn = document.getElementById('btn-room-picker');
  if (!sheet) return;
  const next = forceState !== undefined ? forceState : !sheet.classList.contains('visible');
  sheet.classList.toggle('visible', next);
  if (btn) btn.classList.toggle('active', next);
  if (next) toggleRoomHeaderDropdown(null, false);
};

// ============================================================================
// ROOM ISOLATION (OTHER ROOMS GREYED OUT + 2 WALLS INVISIBLE OR TOP-DOWN)
// ============================================================================
let roomViewMode = 'cutaway'; // 'cutaway' (2 Seiten offen) | 'topdown' (Draufsicht)
let roomOrbitCornerIdx = 0;
let lastCutawayKey = '';

const greyWallMat = new THREE.MeshStandardMaterial({
  color: 0x334155,
  roughness: 0.85,
  metalness: 0.05,
  transparent: true,
  opacity: 0.035,
  depthWrite: false
});
const greyWallCapMat = new THREE.MeshStandardMaterial({
  color: 0x475569,
  roughness: 0.85,
  metalness: 0.05,
  transparent: true,
  opacity: 0.045,
  depthWrite: false
});
const greyFloorMat = new THREE.MeshStandardMaterial({
  color: 0x1e293b,
  roughness: 0.9,
  metalness: 0.05,
  transparent: true,
  opacity: 0.08,
  depthWrite: false
});
const greyLineMat = new THREE.LineBasicMaterial({
  color: 0x475569,
  transparent: true,
  opacity: 0.05
});
const dimmedMatCache = new WeakMap();

function getDimmedFurnitureMat(origMat) {
  if (!origMat) return greyWallMat;
  if (dimmedMatCache.has(origMat)) return dimmedMatCache.get(origMat);
  const cloned = origMat.clone();
  if (cloned.color) {
    cloned.color.lerp(new THREE.Color(0x334155), 0.85);
  }
  cloned.transparent = true;
  cloned.opacity = Math.min(origMat.opacity !== undefined ? origMat.opacity : 1.0, 0.045);
  cloned.depthWrite = false;
  dimmedMatCache.set(origMat, cloned);
  return cloned;
}

function doesPointBelongToRoom(px, py, room, margin = 0) {
  if (!room || !room.bounds) return false;
  const [minX, minY, maxX, maxY] = room.bounds;
  if (px < minX - margin || px > maxX + margin || py < minY - margin || py > maxY + margin) {
    return false;
  }
  // L-shape notch handling for Neues Bad vs Flur
  if (room.id === 'label-bad' && px > 714 + margin && py < 386 - margin) {
    return false;
  }
  if (room.id === 'label-flur2' && px > 684 + margin && py > 362 - margin) {
    return false;
  }
  return true;
}

// Tag all furniture & floor meshes once with their original material and owning roomId
scene.updateMatrixWorld(true);
const orderedRoomsForTagging = [
  ...roomLabels.filter(r => r.id === 'label-bad'),
  ...roomLabels.filter(r => r.id !== 'label-bad')
];

['w1', 'w2', 'common'].forEach(secKey => {
  groups[secKey].floors.traverse(obj => {
    if (obj.isMesh || obj.isLine) {
      obj.userData.origMat = obj.material;
    }
  });
  groups[secKey].details.traverse(obj => {
    if (obj.isMesh || obj.isLine) {
      obj.userData.origMat = obj.material;
      if (obj.isMesh) {
        const box = new THREE.Box3().setFromObject(obj);
        const center = new THREE.Vector3();
        box.getCenter(center);
        const px = center.x / SCALE + CENTER_X;
        const py = center.z / SCALE + CENTER_Y;
        const match = orderedRoomsForTagging.find(r => doesPointBelongToRoom(px, py, r, 4));
        obj.userData.roomId = match ? match.id : null;
      }
    }
  });
});

// Dedicated Floor Spotlight Mesh for the currently isolated room
const activeRoomHighlightGroup = new THREE.Group();
activeRoomHighlightGroup.visible = false;
scene.add(activeRoomHighlightGroup);

function rebuildActiveRoomFloorHighlight(room) {
  while (activeRoomHighlightGroup.children.length > 0) {
    activeRoomHighlightGroup.remove(activeRoomHighlightGroup.children[0]);
  }
  if (!room || !room.bounds) {
    activeRoomHighlightGroup.visible = false;
    return;
  }
  const [minX, minY, maxX, maxY] = room.bounds;
  const pts = room.poly || [
    [minX, minY],
    [maxX, minY],
    [maxX, maxY],
    [minX, maxY]
  ];
  const shape = new THREE.Shape();
  pts.forEach(([px, py], idx) => {
    const sx = toX(px);
    const sy = -toZ(py);
    if (idx === 0) shape.moveTo(sx, sy);
    else shape.lineTo(sx, sy);
  });
  shape.closePath();

  const floorColor = room.id === 'label-bad' ? 0xfed7aa : (room.apt === 'w2' ? 0xd1fae5 : 0xdbeafe);
  const geo = new THREE.ExtrudeGeometry(shape, { depth: 0.025, bevelEnabled: false });
  const mat = new THREE.MeshStandardMaterial({
    color: floorColor,
    roughness: 0.55,
    metalness: 0.05
  });
  const mesh = new THREE.Mesh(geo, mat);
  mesh.rotation.x = -Math.PI / 2;
  mesh.position.y = 0.002;
  mesh.receiveShadow = true;
  activeRoomHighlightGroup.add(mesh);

  const edges = new THREE.EdgesGeometry(geo);
  const lineMat = new THREE.LineBasicMaterial({ color: room.color || '#38bdf8' });
  const line = new THREE.LineSegments(edges, lineMat);
  line.rotation.x = -Math.PI / 2;
  line.position.y = 0.004;
  activeRoomHighlightGroup.add(line);
  activeRoomHighlightGroup.visible = true;
}

function getWallPieceSideInRoom(ud, room) {
  const [minX, minY, maxX, maxY] = room.bounds;
  const rcx = (minX + maxX) / 2;
  const rcy = (minY + maxY) / 2;
  // Special case: Neues Bad L-corner step at North-East
  if (room.id === 'label-bad' && ud.midPy < 388 && ud.midPx > 690) {
    return 'N';
  }
  // Special case: Martins Zimmer 45° Erker – obere Hälfte (mit Rollladen!) gehört zu 'N', untere Hälfte zu 'S'
  if (room.id === 'label-martin' && ud.midPx > 885) {
    return ud.midPy < 387.5 ? 'N' : 'S';
  }
  if (Math.abs(ud.dx) >= Math.abs(ud.dy)) {
    return ud.midPy < rcy ? 'N' : 'S';
  } else {
    return ud.midPx < rcx ? 'W' : 'E';
  }
}

function applyRoomIsolationAndCutaway(forceUpdate = false) {
  if (!activeRoomId) {
    if (lastCutawayKey === 'NONE' && !forceUpdate) return;
    lastCutawayKey = 'NONE';
    activeRoomHighlightGroup.visible = false;

    ['w1', 'w2', 'common'].forEach(secKey => {
      groups[secKey].walls.traverse(obj => {
        if (obj.userData && obj.userData.isWallPiece) {
          obj.visible = true;
          obj.scale.z = 1.0;
          obj.castShadow = !obj.userData.isCap;
          if (obj.userData.origMat) obj.material = obj.userData.origMat;
        }
      });
      groups[secKey].floors.traverse(obj => {
        if (obj.userData && obj.userData.origMat) {
          obj.material = obj.userData.origMat;
        }
      });
      groups[secKey].details.traverse(obj => {
        if (obj.userData && obj.userData.origMat) {
          obj.material = obj.userData.origMat;
          if (obj.isMesh) obj.castShadow = true;
        }
      });
    });
    window.currentHiddenSides = [];
    updateSimonLightVisuals();
    if (typeof updateAllScreensVisuals === 'function') updateAllScreensVisuals();
    if (typeof updateSchlafLedVisibility === 'function') updateSchlafLedVisibility();
    return;
  }

  const room = roomLabels.find(r => r.id === activeRoomId);
  if (!room) return;

  const [minX, minY, maxX, maxY] = room.bounds;
  const rcx3d = toX((minX + maxX) / 2);
  const rcz3d = toZ((minY + maxY) / 2);

  // Determine which sides of the active room face the camera (and become invisible in cutaway mode)
  const hiddenEW = camera.position.x < rcx3d ? 'W' : 'E';
  const hiddenNS = camera.position.z < rcz3d ? 'N' : 'S';
  const hiddenSides = (roomOrbitCornerIdx === 0 && room.defaultHiddenSides && camera.position.z > rcz3d)
    ? room.defaultHiddenSides
    : [hiddenEW, hiddenNS];
  window.currentHiddenSides = hiddenSides;
  const stateKey = `${activeRoomId}:${roomViewMode}:${hiddenSides.join(',')}`;
  if (stateKey === lastCutawayKey && !forceUpdate) return;
  lastCutawayKey = stateKey;

  ['w1', 'w2', 'common'].forEach(secKey => {
    // 1) Floors: grey out shared floor slabs (activeRoomHighlightGroup highlights the selected room)
    groups[secKey].floors.traverse(obj => {
      if (obj.isMesh) {
        obj.material = greyFloorMat;
      }
    });

    // 2) Furniture & details: keep active room furniture 100% vibrant, make all other rooms ultra-transparent
    groups[secKey].details.traverse(obj => {
      if (obj.isLine) {
        obj.material = greyLineMat;
      } else if (obj.isMesh && obj.userData.origMat) {
        if (obj.userData.roomId === activeRoomId) {
          obj.material = obj.userData.origMat;
          obj.castShadow = true;
        } else {
          obj.material = getDimmedFurnitureMat(obj.userData.origMat);
          obj.castShadow = false;
        }
      }
    });

    // 3) Walls: flatten & ultra-fade other rooms' walls so they NEVER overlap the active room;
    //    for the active room, make 2 camera-facing sides invisible in cutaway mode!
    groups[secKey].walls.traverse(obj => {
      if (!obj.userData || !obj.userData.isWallPiece) return;
      const ud = obj.userData;

      if (ud.isRailing) {
        obj.visible = false;
        return;
      }

      let belongsToRoom = doesPointBelongToRoom(ud.midPx, ud.midPy, room, 9.5);
      if (ud.shutterRoomId && ud.shutterRoomId !== activeRoomId) {
        belongsToRoom = false;
      }

      if (!belongsToRoom) {
        // Other room wall -> flatten to a faint floor outline and make ultra-transparent so nothing blocks the view!
        if (ud.isCap || ud.isOpening || (ud.yBottom && ud.yBottom > 0.05)) {
          obj.visible = false;
        } else {
          obj.visible = true;
          obj.scale.z = 0.05;
          obj.castShadow = false;
          obj.material = greyWallMat;
        }
      } else {
        // Active room wall!
        obj.scale.z = 1.0;
        if (roomViewMode === 'topdown') {
          // Draufsicht: all 4 walls of the room visible around it
          obj.visible = true;
          obj.castShadow = !ud.isCap;
          obj.material = ud.origMat;
        } else {
          // 3D Cutaway: camera-facing sides are invisible, back sides stay at full height!
          const side = getWallPieceSideInRoom(ud, room);
          if (hiddenSides.includes(side)) {
            obj.visible = false;
            obj.castShadow = false;
          } else {
            obj.visible = true;
            obj.castShadow = !ud.isCap;
            obj.material = ud.origMat;
          }
        }
      }
    });
  });
  updateSimonLightVisuals();
  if (typeof updateAllScreensVisuals === 'function') updateAllScreensVisuals();
  if (typeof updateSchlafLedVisibility === 'function') updateSchlafLedVisibility();
}

// Passt die Sichtbarkeit der Decken-LED-Leisten im Schlafzimmer dynamisch an:
// Leisten an abgeschnittenen Wänden im 3D-Cutaway werden ausgeblendet, damit nichts im Raum schwebt.
function updateSchlafLedVisibility() {
  if (!window.schlafCeilingLed || !window.schlafCeilingLed.segments) return;
  const isSchlaf = activeRoomId === 'label-schlaf';
  const hidden = (isSchlaf && window.currentHiddenSides) ? window.currentHiddenSides : [];
  window.schlafCeilingLed.segments.forEach(seg => {
    const wallIsHidden = isSchlaf && roomViewMode !== 'topdown' && hidden.includes(seg.side);
    if (wallIsHidden) {
      seg.stripMesh.visible = false;
      if (seg.railMesh) seg.railMesh.visible = false;
      seg.washMesh.visible = false;
    } else {
      const inView = !activeRoomId || isSchlaf;
      seg.stripMesh.visible = inView;
      if (seg.railMesh) seg.railMesh.visible = inView;
      seg.washMesh.visible = inView && window.schlafCeilingLed.isOn;
    }
  });
  if (window.schlafCeilingLed.floorGlowMesh) {
    window.schlafCeilingLed.floorGlowMesh.visible = window.schlafCeilingLed.isOn && (!activeRoomId || isSchlaf);
  }
}

let activeLedTarget = 'simon'; // 'simon' (Tisch-LED) oder 'schlaf' (Decken-LED im Schlafzimmer)

function applySingleRoomLight3D(rl, roomId, badgeId, topBtnId) {
  if (!rl) return;
  const inRoom = activeRoomId === roomId;
  const showConeAndGlow = rl.isOn && (!activeRoomId || inRoom);

  rl.pointLight.intensity = rl.isOn ? (inRoom ? 2.1 : 1.1) : 0;
  rl.coneMesh.visible = showConeAndGlow;
  rl.floorGlowMesh.visible = showConeAndGlow;

  if (rl.isOn) {
    rl.bulbMat.color.setHex(0xfef08a);
    rl.bulbMat.emissive.setHex(0xfacc15);
    rl.bulbMat.emissiveIntensity = 1.35;
  } else {
    rl.bulbMat.color.setHex(0x475569);
    rl.bulbMat.emissive.setHex(0x000000);
    rl.bulbMat.emissiveIntensity = 0;
  }

  if (inRoom && activeRoomHighlightGroup.children[0]) {
    const floorMesh = activeRoomHighlightGroup.children[0];
    if (floorMesh && floorMesh.material) {
      floorMesh.material.color.setHex(rl.isOn ? 0xfef3c7 : 0x64748b);
    }
  }

  const badge = document.getElementById(badgeId);
  const bulbSvg = rl.isOn
    ? `<svg class="gh-tile-icon" viewBox="0 0 24 24" fill="none"><circle cx="12" cy="9.5" r="6.2" fill="currentColor"/><path d="M9.2 17.5h5.6M10.4 20.2h3.2" stroke="currentColor" stroke-width="2.1" stroke-linecap="round"/></svg>`
    : `<svg class="gh-tile-icon" viewBox="0 0 24 24" fill="none"><circle cx="12" cy="9.5" r="5.5" stroke="currentColor" stroke-width="2.1"/><path d="M9.2 17.5h5.6M10.4 20.2h3.2" stroke="currentColor" stroke-width="2.1" stroke-linecap="round"/></svg>`;
  if (badge) {
    badge.classList.toggle('on', rl.isOn);
    badge.innerHTML = `${bulbSvg}<div class="gh-tile-text"><span class="gh-tile-title">Licht</span><span class="gh-tile-sub">${rl.isOn ? 'An' : 'Aus'}</span></div>`;
  }

  const topBtn = document.getElementById(topBtnId);
  if (topBtn) {
    topBtn.classList.toggle('active', rl.isOn);
    topBtn.innerHTML = `${bulbSvg.replace('gh-tile-icon', 'gh-inline-icon')}<span>Licht · ${rl.isOn ? 'An' : 'Aus'}</span>`;
  }
}

function updateSimonLightVisuals() {
  applySingleRoomLight3D(window.simonRoomLight, 'label-simon', 'badge-light-simon', 'top-btn-simon-light');
  applySingleRoomLight3D(window.schlafRoomLight, 'label-schlaf', 'badge-light-schlaf', 'top-btn-schlaf-light');

  // Wenn man in Simons Zimmer oder im Schlafzimmer ist und das Deckenlicht AUS macht, Raum deutlich abdunkeln,
  // damit man den Unterschied zwischen Licht AN, Licht AUS und dem RGB-LED-Band sofort stark sieht!
  const simonDark = activeRoomId === 'label-simon' && window.simonRoomLight && !window.simonRoomLight.isOn;
  const schlafDark = activeRoomId === 'label-schlaf' && window.schlafRoomLight && !window.schlafRoomLight.isOn;
  if (simonDark || schlafDark) {
    hemiLight.intensity = 0.24;
    dirLight.intensity = 0.18;
  } else {
    hemiLight.intensity = 0.82;
    dirLight.intensity = 0.72;
  }

  updateSimonDeskLedVisuals();
}

window.toggleSimonRoomLight = function() {
  if (!window.simonRoomLight) return;
  window.simonRoomLight.isOn = !window.simonRoomLight.isOn;
  updateSimonLightVisuals();
};

window.toggleSchlafRoomLight = function() {
  if (!window.schlafRoomLight) return;
  window.schlafRoomLight.isOn = !window.schlafRoomLight.isOn;
  updateSimonLightVisuals();
};

function applySingleLed3DAndBadge(led, roomId, badgeId, topBtnId, shortTitle) {
  if (!led) return;
  const inRoom = activeRoomId === roomId;
  const showGlow = led.isOn && (!activeRoomId || inRoom);
  const bNorm = Math.max(0.1, Math.min(1, led.brightness / 100));
  const threeCol = new THREE.Color(led.colorHex);

  if (led.underGlowMesh) led.underGlowMesh.visible = showGlow;
  if (led.floorGlowMesh) led.floorGlowMesh.visible = showGlow;

  if (led.isOn) {
    if (led.stripMat) {
      led.stripMat.color.copy(threeCol);
      led.stripMat.emissive.copy(threeCol);
      led.stripMat.emissiveIntensity = 2.0 * bNorm;
    }
    if (led.underGlowMat) {
      led.underGlowMat.color.copy(threeCol);
      led.underGlowMat.opacity = 0.25 * bNorm;
    }
    if (led.washMat) {
      led.washMat.color.copy(threeCol);
      led.washMat.opacity = 0.55 * bNorm;
    }
    if (led.haloMat) {
      led.haloMat.color.copy(threeCol);
      led.haloMat.opacity = 0.18 * bNorm;
    }
    if (led.floorGlowMat) {
      led.floorGlowMat.color.copy(threeCol);
      led.floorGlowMat.opacity = (led.washMat ? 0.28 : (led.haloMat ? 0.25 : 0.44)) * bNorm;
    }
    if (led.pointLight) {
      led.pointLight.color.copy(threeCol);
      led.pointLight.intensity = (inRoom ? 2.2 : 1.1) * bNorm;
    }
  } else {
    if (led.stripMat) {
      led.stripMat.color.setHex(0x334155);
      led.stripMat.emissive.setHex(0x000000);
      led.stripMat.emissiveIntensity = 0;
    }
    if (led.washMat) {
      led.washMat.opacity = 0;
    }
    if (led.floorGlowMesh) {
      led.floorGlowMesh.visible = false;
    }
    if (led.pointLight) {
      led.pointLight.intensity = 0;
    }
  }

  if (typeof updateSchlafLedVisibility === 'function') {
    updateSchlafLedVisibility();
  }

  // Schwebenden LED-Button aktualisieren (Google Home Tile Stil ohne Punkt)
  const badge = document.getElementById(badgeId);
  const ledSvg = led.isOn
    ? `<svg class="gh-tile-icon" viewBox="0 0 24 24" fill="none"><circle cx="12" cy="9.5" r="6.2" fill="currentColor"/><path d="M9.2 17.5h5.6M10.4 20.2h3.2" stroke="currentColor" stroke-width="2.1" stroke-linecap="round"/></svg>`
    : `<svg class="gh-tile-icon" viewBox="0 0 24 24" fill="none"><circle cx="12" cy="9.5" r="5.5" stroke="currentColor" stroke-width="2.1"/><path d="M9.2 17.5h5.6M10.4 20.2h3.2" stroke="currentColor" stroke-width="2.1" stroke-linecap="round"/></svg>`;
  if (badge) {
    badge.classList.toggle('on', led.isOn);
    badge.style.background = '';
    badge.style.borderColor = '';
    badge.style.boxShadow = '';
    badge.style.color = '';
    const subTxt = led.isOn ? `An · ${led.colorName}` : 'Aus';
    badge.innerHTML = `${ledSvg}<div class="gh-tile-text"><span class="gh-tile-title">${shortTitle}</span><span class="gh-tile-sub">${subTxt}</span></div>`;
  }

  // Top-Bar-Button aktualisieren
  const topLedBtn = document.getElementById(topBtnId);
  if (topLedBtn) {
    topLedBtn.classList.toggle('active', led.isOn);
    topLedBtn.innerHTML = led.isOn
      ? `${ledSvg.replace('gh-tile-icon', 'gh-inline-icon')}<span>${shortTitle} · An (${led.colorName})</span>`
      : `${ledSvg.replace('gh-tile-icon', 'gh-inline-icon')}<span>${shortTitle} · Aus</span>`;
  }
}

function getActiveLedObj() {
  return activeLedTarget === 'schlaf' ? window.schlafCeilingLed : window.simonDeskLed;
}

function updateSimonDeskLedVisuals() {
  applySingleLed3DAndBadge(window.simonDeskLed, 'label-simon', 'badge-led-simon', 'top-btn-simon-led', 'Tisch-LED');
  applySingleLed3DAndBadge(window.schlafCeilingLed, 'label-schlaf', 'badge-led-schlaf', 'top-btn-schlaf-led', 'Decken-LED');

  const led = getActiveLedObj();
  if (!led) return;

  // Google Home Popup aktualisieren (Screenshot 1, 2, 3)
  const popTitle = document.getElementById('led-popup-title');
  if (popTitle) {
    popTitle.textContent = activeLedTarget === 'schlaf' ? 'Decken-LED' : 'PC Licht';
  }

  const pillFill = document.getElementById('gh-pill-fill');
  const onContent = document.getElementById('gh-pill-on-content');
  const offContent = document.getElementById('gh-pill-off-content');
  const pctReadout = document.getElementById('gh-pill-pct');
  const customTile = document.getElementById('gh-custom-swatch-tile');

  if (customTile) {
    customTile.style.background = led.colorHex;
  }

  if (pillFill) {
    if (led.isOn) {
      pillFill.style.height = `${Math.round(led.brightness)}%`;
      pillFill.style.background = `linear-gradient(180deg, ${led.colorHex}ee 0%, ${led.colorHex}88 100%)`;
    } else {
      pillFill.style.height = '0%';
    }
  }

  if (onContent && offContent) {
    if (led.isOn) {
      onContent.style.display = 'flex';
      offContent.style.display = 'none';
      if (pctReadout) pctReadout.textContent = `${Math.round(led.brightness)} %`;
    } else {
      onContent.style.display = 'none';
      offContent.style.display = 'flex';
    }
  }
}

// ============================================================================
// GOOGLE HOME MATERIAL 3 CONTROLS & "LICHTFARBE" COLOR WHEEL / TEMP
// ============================================================================
let tempSavedColor = '#a855f7';
let activeColorTab = 'farbe';
let isColorWheelInit = false;

// HSV zu RGB & RGB zu HEX Helfer
function hsvToRgb(h, s, v) {
  const c = v * s;
  const x = c * (1 - Math.abs(((h / 60) % 2) - 1));
  const m = v - c;
  let r = 0, g = 0, b = 0;
  if (h < 60) { r = c; g = x; }
  else if (h < 120) { r = x; g = c; }
  else if (h < 180) { g = c; b = x; }
  else if (h < 240) { g = x; b = c; }
  else if (h < 300) { r = x; b = c; }
  else { r = c; b = x; }
  return [Math.round((r + m) * 255), Math.round((g + m) * 255), Math.round((b + m) * 255)];
}

function rgbToHex(r, g, b) {
  return '#' + [r, g, b].map(x => x.toString(16).padStart(2, '0')).join('');
}

// Kelvin-Farbverlauf (Screenshot 4)
function getTempColorAt(t) {
  t = Math.max(0, Math.min(1, t));
  const stops = [
    { t: 0.00, c: [255, 96, 16] },   // 2000K Warmamber
    { t: 0.25, c: [255, 149, 52] },  // 2700K Warmweiß
    { t: 0.55, c: [255, 228, 181] }, // 3500K Sanftweiß
    { t: 0.75, c: [255, 255, 255] }, // 5000K Neutralweiß
    { t: 1.00, c: [212, 232, 255] }  // 6500K Kaltweiß/Tageslicht
  ];
  for (let i = 0; i < stops.length - 1; i++) {
    if (t >= stops[i].t && t <= stops[i + 1].t) {
      const localT = (t - stops[i].t) / (stops[i + 1].t - stops[i].t);
      const r = Math.round(stops[i].c[0] + (stops[i + 1].c[0] - stops[i].c[0]) * localT);
      const g = Math.round(stops[i].c[1] + (stops[i + 1].c[1] - stops[i].c[1]) * localT);
      const b = Math.round(stops[i].c[2] + (stops[i + 1].c[2] - stops[i].c[2]) * localT);
      return rgbToHex(r, g, b);
    }
  }
  return '#ffffff';
}

// Farbrad rendern (Screenshot 5)
function renderColorWheel() {
  const canvas = document.getElementById('gh-wheel-canvas');
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  const w = canvas.width;
  const h = canvas.height;
  const cx = w / 2;
  const cy = h / 2;
  const radius = cx - 10;
  const imgData = ctx.createImageData(w, h);
  const data = imgData.data;

  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      const dx = x - cx;
      const dy = y - cy;
      const dist = Math.sqrt(dx * dx + dy * dy);
      const idx = (y * w + x) * 4;

      if (dist <= radius) {
        let angle = Math.atan2(dy, dx) * (180 / Math.PI);
        if (angle < 0) angle += 360;
        const sat = Math.min(1, dist / radius);
        const rgb = hsvToRgb(angle, sat, 1.0);
        data[idx] = rgb[0];
        data[idx + 1] = rgb[1];
        data[idx + 2] = rgb[2];
        data[idx + 3] = 255;
      } else if (dist <= radius + 1) {
        data[idx + 3] = Math.round((1 - (dist - radius)) * 255);
      } else {
        data[idx + 3] = 0;
      }
    }
  }
  ctx.putImageData(imgData, 0, 0);
  isColorWheelInit = true;
}

// Öffnet das "Lichtfarbe"-Menü (Screenshot 4 & 5)
window.openLichtfarbeSheet = function() {
  const led = getActiveLedObj();
  if (led) tempSavedColor = led.colorHex;

  const mainView = document.getElementById('gh-led-main-view');
  const colorView = document.getElementById('gh-led-color-view');
  if (mainView) mainView.style.display = 'none';
  if (colorView) colorView.style.display = 'flex';

  if (!isColorWheelInit) {
    renderColorWheel();
  }
  switchColorTab(activeColorTab);
};

// Schließt das "Lichtfarbe"-Menü
window.closeLichtfarbeSheet = function(isConfirmed = true) {
  if (!isConfirmed && tempSavedColor) {
    setSimonDeskLedColor(tempSavedColor, 'Farbe');
  } else {
    const led = getActiveLedObj();
    const customTile = document.getElementById('gh-custom-swatch-tile');
    if (customTile && led) {
      customTile.style.background = led.colorHex;
    }
  }
  const mainView = document.getElementById('gh-led-main-view');
  const colorView = document.getElementById('gh-led-color-view');
  if (colorView) colorView.style.display = 'none';
  if (mainView) mainView.style.display = 'flex';
};

// Wechselt zwischen den Reitern "Farbe" und "Temperatur"
window.switchColorTab = function(tab) {
  activeColorTab = tab;
  const btnFarbe = document.getElementById('tab-btn-farbe');
  const btnTemp = document.getElementById('tab-btn-temp');
  const panelFarbe = document.getElementById('panel-farbe');
  const panelTemp = document.getElementById('panel-temp');

  if (btnFarbe) btnFarbe.classList.toggle('active', tab === 'farbe');
  if (btnTemp) btnTemp.classList.toggle('active', tab === 'temp');
  if (panelFarbe) panelFarbe.style.display = tab === 'farbe' ? 'flex' : 'none';
  if (panelTemp) panelTemp.style.display = tab === 'temp' ? 'flex' : 'none';
};

// Globale Drag-Handler für Kapsel-Dimmer, Farbrad, Temperatur & Rollladen-Kapsel
function initGoogleHomeControls() {
  // 1. Vertikaler Kapsel-Dimmer (Screenshot 3)
  const pillSlider = document.getElementById('gh-pill-slider');
  if (pillSlider) {
    let isDraggingPill = false;
    const updatePillFromPointer = (clientY) => {
      const rect = pillSlider.getBoundingClientRect();
      const relY = rect.bottom - clientY;
      const pct = Math.max(1, Math.min(100, Math.round((relY / rect.height) * 100)));
      setSimonDeskLedBrightness(pct);
    };

    pillSlider.addEventListener('pointerdown', e => {
      if (e.target && e.target.closest && e.target.closest('.gh-pill-bulb-icon, .gh-pill-off-knob')) return;
      isDraggingPill = true;
      pillSlider.setPointerCapture(e.pointerId);
      updatePillFromPointer(e.clientY);
    });

    pillSlider.addEventListener('pointermove', e => {
      if (!isDraggingPill) return;
      updatePillFromPointer(e.clientY);
    });

    const stopPillDrag = e => {
      if (isDraggingPill) {
        isDraggingPill = false;
        try { pillSlider.releasePointerCapture(e.pointerId); } catch(err){}
      }
    };
    pillSlider.addEventListener('pointerup', stopPillDrag);
    pillSlider.addEventListener('pointercancel', stopPillDrag);
  }

  // 2. Vertikale Rollladen-Kapsel
  const shutterPill = document.getElementById('gh-shutter-pill-slider');
  if (shutterPill) {
    let isDraggingShutter = false;
    const updateShutterFromPointer = (clientY) => {
      const rect = shutterPill.getBoundingClientRect();
      const relY = rect.bottom - clientY;
      const pct = Math.max(0, Math.min(100, Math.round((relY / rect.height) * 100)));
      setActiveShutterPercent(pct);
    };

    shutterPill.addEventListener('pointerdown', e => {
      isDraggingShutter = true;
      shutterPill.setPointerCapture(e.pointerId);
      updateShutterFromPointer(e.clientY);
    });

    shutterPill.addEventListener('pointermove', e => {
      if (!isDraggingShutter) return;
      updateShutterFromPointer(e.clientY);
    });

    const stopShutterDrag = e => {
      if (isDraggingShutter) {
        isDraggingShutter = false;
        try { shutterPill.releasePointerCapture(e.pointerId); } catch(err){}
      }
    };
    shutterPill.addEventListener('pointerup', stopShutterDrag);
    shutterPill.addEventListener('pointercancel', stopShutterDrag);
  }

  // 3. Farbrad Picker (Screenshot 5)
  const wheelWrap = document.getElementById('gh-wheel-wrapper');
  const wheelHandle = document.getElementById('gh-wheel-handle');
  if (wheelWrap) {
    let isDraggingWheel = false;
    const pickWheelColor = (clientX, clientY) => {
      const rect = wheelWrap.getBoundingClientRect();
      const cx = rect.left + rect.width / 2;
      const cy = rect.top + rect.height / 2;
      const dx = clientX - cx;
      const dy = clientY - cy;
      const dist = Math.sqrt(dx * dx + dy * dy);
      const maxR = (rect.width / 2) - 10;
      const clampedDist = Math.min(maxR, dist);
      let angle = Math.atan2(dy, dx) * (180 / Math.PI);
      if (angle < 0) angle += 360;
      const sat = Math.min(1, clampedDist / maxR);
      const rgb = hsvToRgb(angle, sat, 1.0);
      const hex = rgbToHex(rgb[0], rgb[1], rgb[2]);

      if (wheelHandle) {
        const hx = (rect.width / 2) + Math.cos(angle * Math.PI / 180) * clampedDist;
        const hy = (rect.height / 2) + Math.sin(angle * Math.PI / 180) * clampedDist;
        wheelHandle.style.left = `${hx}px`;
        wheelHandle.style.top = `${hy}px`;
      }
      setSimonDeskLedColor(hex, 'Farbe');
    };

    wheelWrap.addEventListener('pointerdown', e => {
      isDraggingWheel = true;
      wheelWrap.setPointerCapture(e.pointerId);
      pickWheelColor(e.clientX, e.clientY);
    });

    wheelWrap.addEventListener('pointermove', e => {
      if (!isDraggingWheel) return;
      pickWheelColor(e.clientX, e.clientY);
    });

    const stopWheelDrag = e => {
      if (isDraggingWheel) {
        isDraggingWheel = false;
        try { wheelWrap.releasePointerCapture(e.pointerId); } catch(err){}
      }
    };
    wheelWrap.addEventListener('pointerup', stopWheelDrag);
    wheelWrap.addEventListener('pointercancel', stopWheelDrag);
  }

  // 4. Farbtemperatur Slider (Screenshot 4)
  const tempTrack = document.getElementById('gh-temp-track');
  const tempHandle = document.getElementById('gh-temp-handle');
  if (tempTrack) {
    let isDraggingTemp = false;
    const pickTempColor = (clientY) => {
      const rect = tempTrack.getBoundingClientRect();
      const relY = Math.max(0, Math.min(rect.height, clientY - rect.top));
      const fraction = relY / rect.height;
      const hex = getTempColorAt(fraction);

      if (tempHandle) {
        tempHandle.style.top = `${relY}px`;
      }
      setSimonDeskLedColor(hex, 'Temperatur');
    };

    tempTrack.addEventListener('pointerdown', e => {
      isDraggingTemp = true;
      tempTrack.setPointerCapture(e.pointerId);
      pickTempColor(e.clientY);
    });

    tempTrack.addEventListener('pointermove', e => {
      if (!isDraggingTemp) return;
      pickTempColor(e.clientY);
    });

    const stopTempDrag = e => {
      if (isDraggingTemp) {
        isDraggingTemp = false;
        try { tempTrack.releasePointerCapture(e.pointerId); } catch(err){}
      }
    };
    tempTrack.addEventListener('pointerup', stopTempDrag);
    tempTrack.addEventListener('pointercancel', stopTempDrag);
  }
}

function checkBackdrop() {
  const bd = document.getElementById('popup-backdrop');
  const ledVis = document.getElementById('led-popup')?.classList.contains('visible');
  const shutVis = document.getElementById('shutter-popup')?.classList.contains('visible');
  if (bd && !ledVis && !shutVis) {
    bd.classList.remove('visible');
  }
}

window.closeAllPopups = function() {
  window.closeSimonLedPopup();
  window.closeShutterPopup();
};

window.openSimonLedPopup = function(target = 'simon', forceModal = null) {
  if (window.closeShutterPopup) window.closeShutterPopup();
  activeLedTarget = target;
  const pop = document.getElementById('led-popup');
  const bd = document.getElementById('popup-backdrop');
  if (!pop) return;

  const isTouch = ('ontouchstart' in window) || (navigator.maxTouchPoints > 0) || window.innerWidth <= 840;
  const isModal = forceModal !== null ? forceModal : isTouch;
  pop.classList.toggle('modal-center', isModal);
  pop.classList.add('visible');
  if (bd) {
    if (isModal) bd.classList.add('visible');
    else bd.classList.remove('visible');
  }
  updateSimonDeskLedVisuals();
};

window.openSchlafLedPopup = function(forceModal = null) {
  window.openSimonLedPopup('schlaf', forceModal);
};

window.closeSimonLedPopup = function() {
  const el = document.getElementById('led-popup');
  if (el) el.classList.remove('visible');
  const mv = document.getElementById('gh-led-main-view');
  const cv = document.getElementById('gh-led-color-view');
  if (mv) mv.style.display = 'flex';
  if (cv) cv.style.display = 'none';
  checkBackdrop();
};

window.toggleSpecificLed = function(target = 'simon') {
  activeLedTarget = target;
  const led = getActiveLedObj();
  if (!led) return;
  led.isOn = !led.isOn;
  updateSimonDeskLedVisuals();
};

window.toggleSimonDeskLed = function() {
  const led = getActiveLedObj();
  if (!led) return;
  led.isOn = !led.isOn;
  updateSimonDeskLedVisuals();
};

window.setSimonDeskLedColor = function(hex, name = 'Farbe') {
  const led = getActiveLedObj();
  if (!led) return;
  led.isOn = true;
  led.colorHex = hex;
  led.colorName = name;
  updateSimonDeskLedVisuals();
};

window.setSimonDeskLedBrightness = function(val) {
  const led = getActiveLedObj();
  if (!led) return;
  led.isOn = true;
  led.brightness = Math.max(1, Math.min(100, Math.round(Number(val))));
  updateSimonDeskLedVisuals();
};

function updateAllScreensVisuals() {
  if (!window.screensRegistry) return;
  window.screensRegistry.forEach(scr => {
    const inThisRoom = !activeRoomId || activeRoomId === scr.roomId;
    const tex = scr.kind === 'tv'
      ? (scr.isOn ? screenTextures.tvOn : screenTextures.tvOff)
      : (scr.isOn ? screenTextures.monOn : screenTextures.monOff);

    scr.mat.map = tex;
    scr.mat.emissiveMap = scr.isOn ? tex : null;
    if (scr.isOn) {
      scr.mat.color.setHex(0xffffff);
      scr.mat.emissive.setHex(0xffffff);
      scr.mat.emissiveIntensity = 1.15;
      scr.mat.roughness = 0.12;
      scr.ledMat.color.setHex(0x22c55e); // Grüne Betriebs-LED
    } else {
      scr.mat.color.setHex(0x1e293b);
      scr.mat.emissive.setHex(0x000000);
      scr.mat.emissiveIntensity = 0.0;
      scr.mat.roughness = 0.35;
      scr.ledMat.color.setHex(0xef4444); // Rote Standby-LED
    }
    scr.mat.needsUpdate = true;

    // 3D-Schein vor dem Bildschirm nur zeigen, wenn das Gerät AN ist (und nicht in einem ausgegrauten Nachbarraum)
    scr.glowMesh.visible = Boolean(scr.isOn && inThisRoom);
  });
}

window.toggleScreenPower = function(screenId) {
  const scr = (window.screensRegistry || []).find(s => s.id === screenId);
  if (!scr) return;
  scr.isOn = !scr.isOn;
  updateAllScreensVisuals();
};

function updateSimonPcVisuals() {
  const pc = window.simonPc;
  if (!pc) return;
  if (pc.isOn) {
    pc.ledMat.color.setHex(0x38bdf8);
    pc.ledMat.emissive.setHex(0x0ea5e9);
    pc.ledMat.emissiveIntensity = 1.5;
    pc.powerMat.color.setHex(0x22c55e);
  } else {
    pc.ledMat.color.setHex(0x1e293b);
    pc.ledMat.emissive.setHex(0x000000);
    pc.ledMat.emissiveIntensity = 0;
    pc.powerMat.color.setHex(0xef4444);
  }

  const pcBadge = document.getElementById('badge-pc-simon');
  const pcSvg = pc.isOn
    ? `<svg class="gh-tile-icon" viewBox="0 0 24 24" fill="none"><rect x="3" y="4" width="18" height="12" rx="2.2" fill="currentColor"/><path d="M8 20h8M12 16v4" stroke="currentColor" stroke-width="2.1" stroke-linecap="round"/></svg>`
    : `<svg class="gh-tile-icon" viewBox="0 0 24 24" fill="none"><rect x="3" y="4" width="18" height="12" rx="2.2" stroke="currentColor" stroke-width="2"/><path d="M8 20h8M12 16v4" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>`;
  if (pcBadge) {
    pcBadge.classList.toggle('on', pc.isOn);
    pcBadge.innerHTML = `${pcSvg}<div class="gh-tile-text"><span class="gh-tile-title">PC</span><span class="gh-tile-sub">${pc.isOn ? 'An' : 'Aus'}</span></div>`;
  }

  const topPcBtn = document.getElementById('top-btn-simon-pc');
  if (topPcBtn) {
    topPcBtn.classList.toggle('active', pc.isOn);
    topPcBtn.innerHTML = `${pcSvg.replace('gh-tile-icon', 'gh-inline-icon')}<span>PC · ${pc.isOn ? 'An' : 'Aus'}</span>`;
  }
}

window.toggleSimonPc = function() {
  if (!window.simonPc) return;
  window.simonPc.isOn = !window.simonPc.isOn;
  // Beide PC-Monitore auf Simons Schreibtisch automatisch mit dem PC an- und ausschalten
  (window.screensRegistry || []).forEach(scr => {
    if (scr.id === 'mon-simon-1' || scr.id === 'mon-simon-2') {
      scr.isOn = window.simonPc.isOn;
    }
  });
  updateSimonPcVisuals();
  updateAllScreensVisuals();
};

function updateKuecheSocketVisuals() {
  const sock = window.kuecheSocket;
  if (!sock) return;
  if (sock.isOn) {
    sock.ledMat.color.setHex(0x22c55e);
    sock.ledMat.emissive.setHex(0x22c55e);
    sock.ledMat.emissiveIntensity = 1.6;
  } else {
    sock.ledMat.color.setHex(0xef4444);
    sock.ledMat.emissive.setHex(0xef4444);
    sock.ledMat.emissiveIntensity = 0.9;
  }

  const plugBadge = document.getElementById('badge-plug-kueche');
  const plugSvg = sock.isOn
    ? `<svg class="gh-tile-icon" viewBox="0 0 24 24" fill="none"><path d="M9 3v5M15 3v5" stroke="currentColor" stroke-width="2.1" stroke-linecap="round"/><path d="M6.5 8h11v4.5a5.5 5.5 0 0 1-11 0V8z" fill="currentColor"/><path d="M12 18v3" stroke="currentColor" stroke-width="2.1" stroke-linecap="round"/></svg>`
    : `<svg class="gh-tile-icon" viewBox="0 0 24 24" fill="none"><path d="M9 3v5M15 3v5" stroke="currentColor" stroke-width="2" stroke-linecap="round"/><path d="M6.5 8h11v4.5a5.5 5.5 0 0 1-11 0V8z" stroke="currentColor" stroke-width="2"/><path d="M12 18v3" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>`;
  if (plugBadge) {
    plugBadge.classList.toggle('on', sock.isOn);
    plugBadge.innerHTML = `${plugSvg}<div class="gh-tile-text"><span class="gh-tile-title">Steckdose</span><span class="gh-tile-sub">${sock.isOn ? 'An' : 'Aus'}</span></div>`;
  }

  const topPlugBtn = document.getElementById('top-btn-kueche-plug');
  if (topPlugBtn) {
    topPlugBtn.classList.toggle('active', sock.isOn);
    topPlugBtn.innerHTML = `${plugSvg.replace('gh-tile-icon', 'gh-inline-icon')}<span>Steckdose · ${sock.isOn ? 'An' : 'Aus'}</span>`;
  }
}

window.toggleKuecheSocket = function() {
  if (!window.kuecheSocket) return;
  window.kuecheSocket.isOn = !window.kuecheSocket.isOn;
  updateKuecheSocketVisuals();
};

function toggleRoomHeaderDropdown(e, forceState) {
  if (e && e.stopPropagation) e.stopPropagation();
  const dd = document.getElementById('room-settings-dropdown');
  const btn = document.getElementById('room-settings-btn');
  if (!dd) return;
  const next = forceState !== undefined ? forceState : !dd.classList.contains('visible');
  dd.classList.toggle('visible', next);
  if (btn) btn.classList.toggle('active', next);
}
window.toggleRoomHeaderDropdown = toggleRoomHeaderDropdown;

function updateRoomBarButtons(room) {
  const bar = document.getElementById('room-view-bar');
  const titleEl = document.getElementById('room-view-title');
  const btnsEl = document.getElementById('room-door-buttons');
  titleEl.textContent = room.text;
  btnsEl.innerHTML = '';

  const btnCutaway = document.createElement('button');
  btnCutaway.className = 'door-pill' + (roomViewMode === 'cutaway' ? ' active' : '');
  btnCutaway.innerHTML = `<svg class="gh-inline-icon" viewBox="0 0 24 24" fill="none"><path d="M12 3l8 4.5v9L12 21l-8-4.5v-9L12 3z" stroke="currentColor" stroke-width="2"/><path d="M12 12l8-4.5M12 12v9M12 12L4 7.5" stroke="currentColor" stroke-width="2"/></svg><span>3D Raum (2 Wände)</span>`;
  btnCutaway.onclick = () => enterRoomView(room.id, 'cutaway');
  btnsEl.appendChild(btnCutaway);

  const btnTopDown = document.createElement('button');
  btnTopDown.className = 'door-pill' + (roomViewMode === 'topdown' ? ' active' : '');
  btnTopDown.innerHTML = `<svg class="gh-inline-icon" viewBox="0 0 24 24" fill="none"><path d="M12 5v14M5 12l7 7 7-7" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg><span>Draufsicht</span>`;
  btnTopDown.onclick = () => enterRoomView(room.id, 'topdown');
  btnsEl.appendChild(btnTopDown);

  if (roomViewMode === 'cutaway') {
    const btnRotate = document.createElement('button');
    btnRotate.className = 'door-pill';
    btnRotate.innerHTML = `<svg class="gh-inline-icon" viewBox="0 0 24 24" fill="none"><path d="M21.5 2v6h-6M21.34 15.57a10 10 0 1 1-.57-8.38l6.73-6.19" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg><span>90° drehen</span>`;
    btnRotate.title = 'Kamera um 90° weiterdrehen (wechselt die 2 offenen Wände)';
    btnRotate.onclick = () => rotateRoomCutaway();
    btnsEl.appendChild(btnRotate);
  }

  if (room.id === 'label-simon' && window.simonRoomLight) {
    const btnLight = document.createElement('button');
    btnLight.id = 'top-btn-simon-light';
    btnLight.className = 'door-pill' + (window.simonRoomLight.isOn ? ' active' : '');
    const lSvg = window.simonRoomLight.isOn
      ? `<svg class="gh-inline-icon" viewBox="0 0 24 24" fill="none"><circle cx="12" cy="9.5" r="5.5" fill="currentColor"/><path d="M9.5 17h5M10.5 19.5h3" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>`
      : `<svg class="gh-inline-icon" viewBox="0 0 24 24" fill="none"><circle cx="12" cy="9.5" r="5.5" stroke="currentColor" stroke-width="2"/><path d="M9.5 17h5M10.5 19.5h3" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>`;
    btnLight.innerHTML = `${lSvg}<span>Licht · ${window.simonRoomLight.isOn ? 'An' : 'Aus'}</span>`;
    btnLight.onclick = () => toggleSimonRoomLight();
    btnsEl.appendChild(btnLight);
  }

  if (room.id === 'label-simon' && window.simonDeskLed) {
    const btnLed = document.createElement('button');
    btnLed.id = 'top-btn-simon-led';
    btnLed.className = 'door-pill' + (window.simonDeskLed.isOn ? ' active' : '');
    makeSmartButton(btnLed, {
      onShortClick: () => toggleSimonDeskLed(),
      onLongPress: (isModal) => openSimonLedPopup('simon', isModal)
    });
    btnsEl.appendChild(btnLed);
  }

  if (room.id === 'label-simon' && window.simonPc) {
    const btnPc = document.createElement('button');
    btnPc.id = 'top-btn-simon-pc';
    btnPc.className = 'door-pill' + (window.simonPc.isOn ? ' active' : '');
    const pcSvg = window.simonPc.isOn
      ? `<svg class="gh-inline-icon" viewBox="0 0 24 24" fill="none"><rect x="3" y="4" width="18" height="12" rx="2" fill="currentColor"/><path d="M8 20h8M12 16v4" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>`
      : `<svg class="gh-inline-icon" viewBox="0 0 24 24" fill="none"><rect x="3" y="4" width="18" height="12" rx="2" stroke="currentColor" stroke-width="2"/><path d="M8 20h8M12 16v4" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>`;
    btnPc.innerHTML = `${pcSvg}<span>PC · ${window.simonPc.isOn ? 'An' : 'Aus'}</span>`;
    btnPc.onclick = () => toggleSimonPc();
    btnsEl.appendChild(btnPc);
  }

  if (room.id === 'label-schlaf' && window.schlafRoomLight) {
    const btnLight = document.createElement('button');
    btnLight.id = 'top-btn-schlaf-light';
    btnLight.className = 'door-pill' + (window.schlafRoomLight.isOn ? ' active' : '');
    const sSvg = window.schlafRoomLight.isOn
      ? `<svg class="gh-inline-icon" viewBox="0 0 24 24" fill="none"><circle cx="12" cy="9.5" r="5.5" fill="currentColor"/><path d="M9.5 17h5M10.5 19.5h3" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>`
      : `<svg class="gh-inline-icon" viewBox="0 0 24 24" fill="none"><circle cx="12" cy="9.5" r="5.5" stroke="currentColor" stroke-width="2"/><path d="M9.5 17h5M10.5 19.5h3" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>`;
    btnLight.innerHTML = `${sSvg}<span>Licht · ${window.schlafRoomLight.isOn ? 'An' : 'Aus'}</span>`;
    btnLight.onclick = () => toggleSchlafRoomLight();
    btnsEl.appendChild(btnLight);
  }

  if (room.id === 'label-schlaf' && window.schlafCeilingLed) {
    const btnLed = document.createElement('button');
    btnLed.id = 'top-btn-schlaf-led';
    btnLed.className = 'door-pill' + (window.schlafCeilingLed.isOn ? ' active' : '');
    makeSmartButton(btnLed, {
      onShortClick: () => toggleSpecificLed('schlaf'),
      onLongPress: (isModal) => openSchlafLedPopup(isModal)
    });
    btnsEl.appendChild(btnLed);
  }

  if (room.id === 'label-kueche' && window.kuecheSocket) {
    const btnPlug = document.createElement('button');
    btnPlug.id = 'top-btn-kueche-plug';
    btnPlug.className = 'door-pill' + (window.kuecheSocket.isOn ? ' active' : '');
    const plugSvg = window.kuecheSocket.isOn
      ? `<svg class="gh-inline-icon" viewBox="0 0 24 24" fill="none"><path d="M9 3v5M15 3v5" stroke="currentColor" stroke-width="2" stroke-linecap="round"/><path d="M6.5 8h11v4.5a5.5 5.5 0 0 1-11 0V8z" fill="currentColor"/><path d="M12 18v3" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>`
      : `<svg class="gh-inline-icon" viewBox="0 0 24 24" fill="none"><path d="M9 3v5M15 3v5" stroke="currentColor" stroke-width="2" stroke-linecap="round"/><path d="M6.5 8h11v4.5a5.5 5.5 0 0 1-11 0V8z" stroke="currentColor" stroke-width="2"/><path d="M12 18v3" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>`;
    btnPlug.innerHTML = `${plugSvg}<span>Steckdose · ${window.kuecheSocket.isOn ? 'An' : 'Aus'}</span>`;
    btnPlug.onclick = () => toggleKuecheSocket();
    btnsEl.appendChild(btnPlug);
  }

  // Rollläden dieses Raumes ebenfalls in die Raum-Aktionen aufnehmen
  const roomShutters = (window.shuttersRegistry || []).filter(s => s.roomId === room.id);
  roomShutters.forEach(sh => {
    const btnSh = document.createElement('button');
    btnSh.id = `top-btn-shutter-${sh.id}`;
    const pct = Math.round(sh.currentPercent);
    btnSh.className = 'door-pill' + (pct < 100 ? ' active' : '');
    const shSvg = `<svg class="gh-inline-icon" viewBox="0 0 24 24" fill="none"><rect x="4" y="4" width="16" height="16" rx="2" stroke="currentColor" stroke-width="2"/><path d="M4 10h16M4 15h16" stroke="currentColor" stroke-width="1.8"/></svg>`;
    btnSh.innerHTML = `${shSvg}<span>${sh.shortLabel || sh.name}: ${pct === 0 ? 'Offen · 0%' : (pct === 100 ? 'Zu · 100%' : pct + '%')}</span>`;
    makeSmartButton(btnSh, {
      onShortClick: () => window.toggleShutterQuick(sh.id),
      onLongPress: (isModal) => openShutterPopup(sh.id, isModal)
    });
    btnsEl.appendChild(btnSh);
  });

  const divEl = document.getElementById('room-dropdown-divider');
  const exitBtn = document.getElementById('room-exit-btn');
  if (divEl) divEl.style.display = 'block';
  if (btnsEl) btnsEl.style.display = 'flex';
  if (exitBtn) exitBtn.style.display = 'flex';
  updateSimonLightVisuals();
  updateSimonPcVisuals();
  updateKuecheSocketVisuals();
  updateAllScreensVisuals();
}

window.rotateRoomCutaway = function() {
  if (!activeRoomId) return;
  roomOrbitCornerIdx = (roomOrbitCornerIdx + 1) % 4;
  enterRoomView(activeRoomId, 'cutaway', true);
};

window.enterRoomView = function(roomId, mode = 'cutaway', keepCornerIdx = false) {
  const room = roomLabels.find(r => r.id === roomId);
  if (!room || !room.bounds || !CONTROL_ROOM_IDS.has(roomId)) return;

  toggleRoomPickerSheet(false);
  toggleRoomHeaderDropdown(null, false);
  if (activeRoomId !== roomId) {
    toggleRoomHeaderDropdown(null, false);
    if (window.closeShutterPopup) window.closeShutterPopup();
  }
  if (activeRoomId !== roomId && !keepCornerIdx) {
    roomOrbitCornerIdx = 0;
  }

  activeRoomId = roomId;
  roomViewMode = (mode === 'topdown') ? 'topdown' : 'cutaway';

  if (wallHeightState !== 2) {
    wallHeightState = 2;
    targetWallScaleY = 1.0;
    document.getElementById('btn-wall-height').textContent = 'Wände: Voll';
  }

  // Highlight active room label
  roomLabels.forEach(r => {
    const el = document.getElementById(r.id);
    if (el) el.classList.toggle('active-room', r.id === roomId);
  });

  rebuildActiveRoomFloorHighlight(room);
  updateRoomBarButtons(room);

  const [minX, minY, maxX, maxY] = room.bounds;
  const rcx3d = toX((minX + maxX) / 2);
  const rcz3d = toZ((minY + maxY) / 2);
  const rw = (maxX - minX) * SCALE;
  const rd = (maxY - minY) * SCALE;
  const rSpan = Math.max(rw, rd, 3.4);
  const zm = getPortraitZoomMult();

  if (roomViewMode === 'topdown') {
    const topHeight = Math.max(7.2, rSpan * 1.65 * zm);
    const camPos = new THREE.Vector3(rcx3d, topHeight, rcz3d + 0.01);
    const lookTarget = new THREE.Vector3(rcx3d, 0.2, rcz3d);
    applyRoomIsolationAndCutaway(true);
    animateCameraTo(camPos, lookTarget, 750, 40);
  } else {
    // Compute diagonal corner direction (rotated by roomOrbitCornerIdx * 90°)
    const baseDir = room.cutawayDir || [-1, 1];
    const corners = [
      [baseDir[0], baseDir[1]],
      [-baseDir[1], baseDir[0]],
      [-baseDir[0], -baseDir[1]],
      [baseDir[1], -baseDir[0]]
    ];
    const [dirX, dirZ] = corners[roomOrbitCornerIdx % 4];
    const dist = Math.max(4.6, rSpan * 1.15 * zm);
    const camY = Math.max(4.4, rSpan * 0.96 * zm);

    const camPos = new THREE.Vector3(rcx3d + dirX * dist, camY, rcz3d + dirZ * dist);
    const lookTarget = new THREE.Vector3(rcx3d, 0.65, rcz3d);
    applyRoomIsolationAndCutaway(true);
    animateCameraTo(camPos, lookTarget, 800, 42);
  }
};

function resetTopBarToOverview() {
  const titleEl = document.getElementById('room-view-title');
  const divEl = document.getElementById('room-dropdown-divider');
  const btnsEl = document.getElementById('room-door-buttons');
  const exitBtn = document.getElementById('room-exit-btn');
  if (titleEl) titleEl.textContent = 'Übersicht';
  if (divEl) divEl.style.display = 'none';
  if (btnsEl) {
    btnsEl.style.display = 'none';
    btnsEl.innerHTML = '';
  }
  if (exitBtn) exitBtn.style.display = 'none';
}

window.exitRoomView = function() {
  activeRoomId = null;
  toggleRoomHeaderDropdown(null, false);
  if (window.closeShutterPopup) window.closeShutterPopup();
  resetTopBarToOverview();
  roomLabels.forEach(r => {
    const el = document.getElementById(r.id);
    if (el) el.classList.remove('active-room');
  });
  applyRoomIsolationAndCutaway(true);
  // Restore normal lighting if leaving Simons Zimmer
  updateSimonLightVisuals();
  setApartmentFilter(currentFilter);
};

// ESC key returns to overview
window.addEventListener('keydown', e => {
  if (e.key === 'Escape' && activeRoomId) {
    exitRoomView();
  }
});

// Direct 3D Floor Click / Tap: tapping inside any room on the 3D floor also flies to its doorway!
const raycaster = new THREE.Raycaster();
const pointerDownPos = { x: 0, y: 0 };
renderer.domElement.addEventListener('pointerdown', e => {
  pointerDownPos.x = e.clientX;
  pointerDownPos.y = e.clientY;
});
renderer.domElement.addEventListener('pointerup', e => {
  if (e.pointerType === 'mouse' && e.button !== 0) return;
  const moveDist = Math.hypot(e.clientX - pointerDownPos.x, e.clientY - pointerDownPos.y);
  const maxTapMove = e.pointerType === 'touch' ? 14 : 6;
  if (moveDist > maxTapMove) return; // Ignore camera drag/orbit gestures

  toggleRoomPickerSheet(false);
  toggleRoomHeaderDropdown(null, false);

  const mouse = new THREE.Vector2(
    (e.clientX / window.innerWidth) * 2 - 1,
    -(e.clientY / window.innerHeight) * 2 + 1
  );
  raycaster.setFromCamera(mouse, camera);

  // Wenn man in einem Raum ist: Direkter Klick auf 3D-Objekte im Raum schaltet diese sofort um!
  if (activeRoomId) {
    const isTouch = e.pointerType === 'touch' || (window.matchMedia && window.matchMedia('(pointer: coarse)').matches && window.innerWidth <= 1024);

    // 1. Screens / Monitore
    const roomScreens = (window.screensRegistry || []).filter(s => s.roomId === activeRoomId);
    const screenMeshes = roomScreens.map(s => s.mesh).filter(Boolean);
    const screenHits = raycaster.intersectObjects(screenMeshes, false);
    if (screenHits.length > 0) {
      const hitScr = roomScreens.find(s => s.mesh === screenHits[0].object);
      if (hitScr) toggleScreenPower(hitScr.id);
      return;
    }

    // 2. 3D Rollläden (Fenster / Tür Vorhänge & Lamellen) direkt anklicken zum Auf/Zu-Toggeln
    const roomShutters = (window.shuttersRegistry || []).filter(s => s.roomId === activeRoomId);
    const shutterMeshes = [];
    roomShutters.forEach(sh => {
      if (sh.curtainMesh) {
        sh.curtainMesh.userData.shId = sh.id;
        shutterMeshes.push(sh.curtainMesh);
      }
      if (sh.bottomBarMesh) {
        sh.bottomBarMesh.userData.shId = sh.id;
        shutterMeshes.push(sh.bottomBarMesh);
      }
    });
    const shutterHits = raycaster.intersectObjects(shutterMeshes, false);
    if (shutterHits.length > 0) {
      const shId = shutterHits[0].object.userData.shId;
      if (shId && window.toggleShutterQuick) {
        window.toggleShutterQuick(shId);
        if (!isTouch && window.openShutterPopup) {
          window.openShutterPopup(shId, false);
        }
      }
      return;
    }

    // 3. 3D Schreibtisch & Tisch-LED in Simons Zimmer
    if (activeRoomId === 'label-simon' && window.simonDeskLed) {
      const deskMeshes = (window.simonDeskLed.meshes || []).filter(Boolean);
      const deskHits = raycaster.intersectObjects(deskMeshes, true);
      if (deskHits.length > 0) {
        toggleSimonDeskLed();
        if (!isTouch && window.openSimonLedPopup) {
          window.openSimonLedPopup('simon', false);
        }
        return;
      }
    }

    // 4. 3D Decken-LED im Schlafzimmer
    if (activeRoomId === 'label-schlaf' && window.schlafCeilingLed) {
      const ceilingMeshes = (window.schlafCeilingLed.meshes || []).filter(Boolean);
      const cHits = raycaster.intersectObjects(ceilingMeshes, true);
      if (cHits.length > 0) {
        window.toggleSpecificLed('schlaf');
        if (!isTouch && window.openSchlafLedPopup) {
          window.openSchlafLedPopup(false);
        }
        return;
      }
    }

    // 5. 3D Deckenlampen (Simons Zimmer & Schlafzimmer)
    if (activeRoomId === 'label-simon' && window.simonRoomLight) {
      const lMeshes = (window.simonRoomLight.meshes || []).filter(Boolean);
      const lHits = raycaster.intersectObjects(lMeshes, false);
      if (lHits.length > 0) {
        toggleSimonRoomLight();
        return;
      }
    }
    if (activeRoomId === 'label-schlaf' && window.schlafRoomLight) {
      const lMeshes = (window.schlafRoomLight.meshes || []).filter(Boolean);
      const lHits = raycaster.intersectObjects(lMeshes, false);
      if (lHits.length > 0) {
        toggleSchlafRoomLight();
        return;
      }
    }

    // 6. 3D PC-Tower
    if (activeRoomId === 'label-simon' && window.simonPc) {
      const pcMeshes = (window.simonPc.meshes || []).filter(Boolean);
      const pcHits = raycaster.intersectObjects(pcMeshes, false);
      if (pcHits.length > 0) {
        toggleSimonPc();
        return;
      }
    }

    // 7. 3D Küchen-Steckdose
    if (activeRoomId === 'label-kueche' && window.kuecheSocket) {
      const sockMeshes = (window.kuecheSocket.meshes || []).filter(Boolean);
      const sockHits = raycaster.intersectObjects(sockMeshes, false);
      if (sockHits.length > 0) {
        toggleKuecheSocket();
        return;
      }
    }

    return;
  }

  const floorPlane = new THREE.Plane(new THREE.Vector3(0, 1, 0), 0);
  const hitPoint = new THREE.Vector3();
  if (raycaster.ray.intersectPlane(floorPlane, hitPoint)) {
    const px = hitPoint.x / SCALE + CENTER_X;
    const py = hitPoint.z / SCALE + CENTER_Y;
    // Find matching room by bounds (check smaller/specific rooms like Neues Bad first)
    const orderedRooms = [
      ...roomLabels.filter(r => r.id === 'label-bad'),
      ...roomLabels.filter(r => r.id !== 'label-bad')
    ];
    const hitRoom = orderedRooms.find(r =>
      r.bounds && px >= r.bounds[0] && px <= r.bounds[2] && py >= r.bounds[1] && py <= r.bounds[3]
    );
    if (hitRoom && activeRoomId !== hitRoom.id) {
      enterRoomView(hitRoom.id, 'cutaway');
    }
  }
});

// Automatically adjust overview distance on narrow smartphone portrait screens so nothing is cut off
function getPortraitZoomMult() {
  const aspect = window.innerWidth / Math.max(1, window.innerHeight);
  return aspect < 0.75 ? 1.38 : (aspect < 1.0 ? 1.18 : 1.0);
}

window.setApartmentFilter = function(filter) {
  currentFilter = filter;
  activeRoomId = null;
  if (window.closeShutterPopup) window.closeShutterPopup();
  resetTopBarToOverview();

  document.querySelectorAll('.hud-btn[data-filter]').forEach(btn => {
    btn.classList.toggle('active', btn.getAttribute('data-filter') === filter);
  });

  const lw1 = document.getElementById('label-w1');
  const lw2 = document.getElementById('label-w2');
  if (lw1) lw1.classList.toggle('dimmed', filter === 'w2');
  if (lw2) lw2.classList.toggle('dimmed', filter === 'w1');
  roomLabels.forEach(r => {
    const el = document.getElementById(r.id);
    if (!el) return;
    el.classList.remove('active-room');
    el.classList.toggle('dimmed', r.apt === 'w2' ? filter === 'w1' : filter === 'w2');
  });

  applyRoomIsolationAndCutaway(true);
  refreshAllSectionMaterials();
  const zm = getPortraitZoomMult();

  if (filter === 'all') {
    const focus = new THREE.Vector3(toX(715), 0, toZ(415));
    const pos = is2DView
      ? new THREE.Vector3(focus.x, 36 * zm, focus.z + 0.01)
      : new THREE.Vector3(focus.x - 9 * zm, 29 * zm, focus.z + 20 * zm);
    animateCameraTo(pos, focus, 750, 40);
  } else if (filter === 'w1') {
    const focus = new THREE.Vector3(toX(775), 0, toZ(280));
    const pos = is2DView
      ? new THREE.Vector3(focus.x, 26 * zm, focus.z + 0.01)
      : new THREE.Vector3(focus.x - 6 * zm, 20 * zm, focus.z + 14 * zm);
    animateCameraTo(pos, focus, 750, 40);
  } else if (filter === 'w2') {
    const focus = new THREE.Vector3(toX(715), 0, toZ(545));
    const pos = is2DView
      ? new THREE.Vector3(focus.x, 26 * zm, focus.z + 0.01)
      : new THREE.Vector3(focus.x - 6 * zm, 20 * zm, focus.z + 15 * zm);
    animateCameraTo(pos, focus, 750, 40);
  }
};

window.toggleCameraPerspective = function() {
  is2DView = !is2DView;
  const btn = document.getElementById('btn-view-mode');
  btn.textContent = is2DView ? '2D Draufsicht' : '3D Ansicht';
  btn.classList.toggle('active', is2DView);

  if (activeRoomId) {
    activeRoomId = null;
    if (window.closeShutterPopup) window.closeShutterPopup();
    resetTopBarToOverview();
    applyRoomIsolationAndCutaway(true);
  }

  const target = new THREE.Vector3(toX(690), 0, toZ(420));
  if (is2DView) {
    animateCameraTo(new THREE.Vector3(target.x, 36, target.z + 0.01), target, 700, 40);
  } else {
    animateCameraTo(new THREE.Vector3(target.x - 8, 28, target.z + 18), target, 700, 40);
  }
};

window.cycleWallHeight = function() {
  wallHeightState = (wallHeightState + 1) % 3;
  const btn = document.getElementById('btn-wall-height');
  if (wallHeightState === 2) {
    targetWallScaleY = 1.0;
    btn.textContent = 'Wände: Voll';
  } else if (wallHeightState === 1) {
    targetWallScaleY = 0.42;
    btn.textContent = 'Wände: Halb';
  } else {
    targetWallScaleY = 0.06;
    btn.textContent = 'Wände: Flach';
  }
};

const labelAnchors = [
  ...roomLabels.map(r => ({ id: r.id, pos: new THREE.Vector3(toX(r.x), 1.6, toZ(r.z)) }))
];

function updateFloatingLabels() {
  const labelsLayer = document.getElementById('labels-layer');
  if (activeRoomId) {
    if (labelsLayer) labelsLayer.style.display = 'none';
    return;
  }
  if (labelsLayer) labelsLayer.style.display = 'block';

  const halfW = window.innerWidth / 2;
  const halfH = window.innerHeight / 2;

  labelAnchors.forEach(item => {
    const el = document.getElementById(item.id);
    if (!el) return;
    const projected = item.pos.clone();
    projected.y *= groups.w1.walls.scale.y;
    projected.project(camera);

    if (projected.z > 1) {
      el.style.display = 'none';
      return;
    }
    el.style.display = 'inline-flex';
    const x = (projected.x * halfW) + halfW;
    const y = -(projected.y * halfH) + halfH;
    el.style.left = `${x}px`;
    el.style.top = `${y}px`;
  });
}

// ============================================================================
// SMART BUTTON INTERACTIONS: SHORT CLICK (TOGGLE) VS. LONG PRESS / PC SIDEBAR
// PC (Maus): Klick toggelt SOFORT (An/Aus bzw. Auf/Zu) UND öffnet gleichzeitig rechts das Menü!
// Handy/Tablet (Touch): Klick toggelt NUR (An/Aus bzw. Auf/Zu) · Halten (Long-Press) öffnet zentriertes Pop-up!
// ============================================================================
function makeSmartButton(el, { onShortClick, onLongPress }) {
  let pressTimer = null;
  let isLongPress = false;
  let startX = 0, startY = 0;
  let lastPointerType = 'mouse';
  const LONG_PRESS_DURATION = 350; // ms

  el.addEventListener('pointerdown', e => {
    if (e.pointerType === 'mouse' && e.button !== 0) return;
    isLongPress = false;
    startX = e.clientX;
    startY = e.clientY;
    lastPointerType = e.pointerType || (e.touches ? 'touch' : 'mouse');

    if (lastPointerType === 'mouse') {
      return; // Mouse clicks on PC will toggle + open side menu in click listener
    }

    el.classList.add('holding');
    pressTimer = setTimeout(() => {
      isLongPress = true;
      el.classList.remove('holding');
      if (navigator.vibrate) {
        try { navigator.vibrate(40); } catch(err){}
      }
      if (onLongPress) {
        onLongPress(true /* forceModal */);
      } else if (onShortClick) {
        onShortClick();
      }
    }, LONG_PRESS_DURATION);
  });

  const cancelHold = () => {
    if (pressTimer) {
      clearTimeout(pressTimer);
      pressTimer = null;
    }
    el.classList.remove('holding');
  };

  el.addEventListener('pointermove', e => {
    if (Math.hypot(e.clientX - startX, e.clientY - startY) > 12) {
      cancelHold();
    }
  });

  el.addEventListener('pointerup', cancelHold);
  el.addEventListener('pointercancel', cancelHold);

  el.addEventListener('click', e => {
    e.stopPropagation();
    if (isLongPress) {
      isLongPress = false;
      return;
    }
    const isTouch = (lastPointerType === 'touch') || (lastPointerType === 'pen') ||
      (e.pointerType === 'touch') ||
      (window.matchMedia && window.matchMedia('(pointer: coarse)').matches && window.innerWidth <= 1024);

    if (!isTouch) {
      // Am PC: Klick toggelt SOFORT (an/aus bzw. auf/zu) UND öffnet gleichzeitig rechts das Menü!
      if (onShortClick) onShortClick();
      if (onLongPress) onLongPress(false /* side menu on PC */);
    } else {
      // Am Handy / Tablet: Kurzer Fingertipp toggelt NUR direkt an/aus bzw. auf/zu!
      // (Farbmenü / Schieberegler öffnet sich am Handy erst durch langes Draufdrücken)
      if (onShortClick) onShortClick();
    }
  });

  // Doppelklick oder Rechtsklick am PC öffnet ebenfalls das Menü
  el.addEventListener('dblclick', e => {
    e.stopPropagation();
    if (onLongPress) onLongPress(false);
  });

  el.addEventListener('contextmenu', e => {
    e.preventDefault();
    e.stopPropagation();
    if (onLongPress) onLongPress(true /* forceModal */);
  });
}

// ============================================================================
// AUTOMATIC ROLLER SHUTTERS (ROLLLÄDEN) UI & 3D ANIMATION
// ============================================================================
let activeShutterId = null;

window.toggleShutterQuick = function(shId) {
  const sh = (window.shuttersRegistry || []).find(s => s.id === shId);
  if (!sh) return;
  const t = (sh.targetPercent !== undefined) ? sh.targetPercent : Math.round(sh.currentPercent);
  const newTarget = t > 20 ? 0 : 100;
  setActiveShutterPercent(newTarget, sh.id);
};

function initShuttersUI() {
  const layer = document.getElementById('shutters-layer');
  if (!layer || !window.shuttersRegistry) return;
  layer.innerHTML = '';

  window.shuttersRegistry.forEach(sh => {
    const btn = document.createElement('div');
    btn.className = 'shutter-badge';
    btn.id = `badge-${sh.id}`;
    btn.title = `${sh.name} steuern (PC: Klick toggelt + öffnet Menü · Handy: Klick toggelt, Halten=Regler)`;
    const lbl = sh.shortLabel || 'Rollladen';
    btn.innerHTML = `
      <div class="shutter-mini-win">
        <div class="shutter-mini-fill" id="mini-fill-${sh.id}" style="height: ${Math.round(sh.currentPercent)}%;"></div>
      </div>
      <div class="gh-tile-text">
        <span class="gh-tile-title" id="badge-title-${sh.id}">${lbl}</span>
        <span class="gh-tile-sub" id="badge-text-${sh.id}">${Math.round(sh.currentPercent)}%</span>
      </div>
    `;

    makeSmartButton(btn, {
      onShortClick: () => {
        // Wechselt direkt zwischen 0% und 100%
        window.toggleShutterQuick(sh.id);
      },
      onLongPress: (isModal) => {
        // Am PC bei Klick oder am Handy bei Long-Press: Sidebar / Pop-up öffnen!
        openShutterPopup(sh.id, isModal);
      }
    });

    layer.appendChild(btn);
    updateSingleShutter3D(sh);
  });

  // Schwebender 3D-Lichtschalter-Button direkt an der Deckenlampe in Simons Zimmer
  if (window.simonRoomLight) {
    const lightBtn = document.createElement('div');
    lightBtn.className = 'light-badge on';
    lightBtn.id = 'badge-light-simon';
    lightBtn.title = 'Deckenlicht in Simons Zimmer an- und ausschalten';
    makeSmartButton(lightBtn, {
      onShortClick: () => toggleSimonRoomLight()
    });
    layer.appendChild(lightBtn);
  }

  // Schwebender 3D-Button für die RGB Tisch-LED direkt unter Simons Schreibtisch
  if (window.simonDeskLed) {
    const ledBtn = document.createElement('div');
    ledBtn.className = 'light-badge on';
    ledBtn.id = 'badge-led-simon';
    ledBtn.title = 'RGB Tisch-LED (PC: Klick toggelt + öffnet Menü · Handy: Klick toggelt, Halten=Farbmenü)';
    makeSmartButton(ledBtn, {
      onShortClick: () => {
        toggleSimonDeskLed();
      },
      onLongPress: (isModal) => {
        openSimonLedPopup('simon', isModal);
      }
    });
    layer.appendChild(ledBtn);
  }

  // Schwebender 3D-Button für Simons PC (schaltet den PC-Tower + beide Monitore an und aus)
  if (window.simonPc) {
    const pcBtn = document.createElement('div');
    pcBtn.className = 'screen-badge on';
    pcBtn.id = 'badge-pc-simon';
    pcBtn.title = 'Simons PC (inkl. beider Monitore) an- und ausschalten';
    makeSmartButton(pcBtn, {
      onShortClick: () => toggleSimonPc()
    });
    layer.appendChild(pcBtn);
  }

  // Schwebender 3D-Lichtschalter-Button direkt an der Deckenlampe im Schlafzimmer
  if (window.schlafRoomLight) {
    const sLightBtn = document.createElement('div');
    sLightBtn.className = 'light-badge on';
    sLightBtn.id = 'badge-light-schlaf';
    sLightBtn.title = 'Deckenlicht im Schlafzimmer an- und ausschalten';
    makeSmartButton(sLightBtn, {
      onShortClick: () => toggleSchlafRoomLight()
    });
    layer.appendChild(sLightBtn);
  }

  // Schwebender 3D-Button für das umlaufende RGB Decken-LED-Band im Schlafzimmer
  if (window.schlafCeilingLed) {
    const sLedBtn = document.createElement('div');
    sLedBtn.className = 'light-badge on';
    sLedBtn.id = 'badge-led-schlaf';
    sLedBtn.title = 'Decken-LED-Band (PC: Klick toggelt + öffnet Menü · Handy: Klick toggelt, Halten=Farbmenü)';
    makeSmartButton(sLedBtn, {
      onShortClick: () => {
        toggleSpecificLed('schlaf');
      },
      onLongPress: (isModal) => {
        openSchlafLedPopup(isModal);
      }
    });
    layer.appendChild(sLedBtn);
  }

  // Schwebender 3D-Button für die schaltbare Steckdose in der Küche
  if (window.kuecheSocket) {
    const kPlugBtn = document.createElement('div');
    kPlugBtn.className = 'screen-badge on';
    kPlugBtn.id = 'badge-plug-kueche';
    kPlugBtn.title = 'Steckdose an der Küchenwand neben dem Tisch an- und ausschalten';
    makeSmartButton(kPlugBtn, {
      onShortClick: () => toggleKuecheSocket()
    });
    layer.appendChild(kPlugBtn);
  }

  updateSimonLightVisuals();
  updateSimonPcVisuals();
  updateKuecheSocketVisuals();
  updateAllScreensVisuals();
}

function updateSingleShutter3D(sh) {
  const pct = Math.max(0, Math.min(100, sh.currentPercent)) / 100;
  const minH = 0.04;
  const curH = Math.max(minH, pct * (sh.winHeight - 0.04));
  sh.curtainMesh.scale.y = curH;
  sh.curtainMesh.position.set(sh.centerLocalX, sh.winTop - curH / 2, 0);
  sh.bottomBarMesh.position.set(sh.centerLocalX, sh.winTop - curH - 0.015, 0);

  // Update mini badge UI
  const badgeEl = document.getElementById(`badge-${sh.id}`);
  const fillEl = document.getElementById(`mini-fill-${sh.id}`);
  const txtEl = document.getElementById(`badge-text-${sh.id}`);
  const rounded = Math.round(sh.currentPercent);
  if (badgeEl) badgeEl.classList.toggle('on', rounded < 100);
  if (fillEl) fillEl.style.height = `${rounded}%`;
  if (txtEl) {
    txtEl.textContent = rounded === 0 ? 'Offen · 0%' : (rounded === 100 ? 'Zu · 100%' : `${rounded}%`);
  }
  const topShBtn = document.getElementById(`top-btn-shutter-${sh.id}`);
  if (topShBtn) {
    topShBtn.classList.toggle('active', rounded < 100);
    topShBtn.innerHTML = `<span>${sh.shortLabel || sh.name}: ${rounded === 0 ? 'Offen · 0%' : (rounded === 100 ? 'Zu · 100%' : rounded + '%')}</span>`;
  }

  // If this shutter is open in the popup, sync popup visuals & tactile slider bar
  if (activeShutterId === sh.id) {
    const popPct = document.getElementById('shutter-popup-pct');
    const popState = document.getElementById('shutter-popup-state');
    const ghPillFill = document.getElementById('gh-shutter-pill-fill');
    const popCurtain = document.getElementById('shutter-preview-curtain');
    const fillBar = document.getElementById('shutter-slider-fill');
    const textBar = document.getElementById('shutter-slider-text');
    const readOut = document.getElementById('shutter-slider-readout');
    const rangeInp = document.getElementById('shutter-range-input');

    if (ghPillFill) ghPillFill.style.height = `${rounded}%`;
    if (popPct) popPct.textContent = `${rounded}%`;
    if (popCurtain) popCurtain.style.height = `${rounded}%`;
    if (fillBar) fillBar.style.width = `${rounded}%`;
    if (textBar) textBar.textContent = rounded === 0 ? 'Offen · 0%' : (rounded === 100 ? 'Zu · 100%' : `${rounded}%`);
    if (readOut) readOut.textContent = `${rounded}%`;
    if (rangeInp && Math.abs(Number(rangeInp.value) - rounded) > 1 && !rangeInp.matches(':active')) {
      rangeInp.value = rounded;
    }
    if (popState) {
      popState.textContent =
        rounded === 0 ? 'Komplett geöffnet (0%)' :
        rounded === 100 ? 'Komplett geschlossen (100%)' :
        `Zu ${rounded}% geschlossen`;
    }
  }
}

window.openShutterPopup = function(shutterId, forceModal = null) {
  if (window.closeSimonLedPopup) window.closeSimonLedPopup();
  const sh = (window.shuttersRegistry || []).find(s => s.id === shutterId);
  if (!sh) return;
  activeShutterId = shutterId;
  const pop = document.getElementById('shutter-popup');
  const bd = document.getElementById('popup-backdrop');
  if (!pop) return;

  const isTouch = ('ontouchstart' in window) || (navigator.maxTouchPoints > 0) || window.innerWidth <= 840;
  const isModal = forceModal !== null ? forceModal : isTouch;
  pop.classList.toggle('modal-center', isModal);

  document.getElementById('shutter-popup-title').textContent = sh.name;
  const rangeInp = document.getElementById('shutter-range-input');
  if (rangeInp) rangeInp.value = Math.round(sh.targetPercent);
  pop.classList.add('visible');
  if (bd) {
    if (isModal) bd.classList.add('visible');
    else bd.classList.remove('visible');
  }
  updateSingleShutter3D(sh);
};

window.closeShutterPopup = function() {
  activeShutterId = null;
  const el = document.getElementById('shutter-popup');
  if (el) el.classList.remove('visible');
  checkBackdrop();
};

window.setActiveShutterPercent = function(pct, optionalShId) {
  const targetId = optionalShId || activeShutterId;
  if (!targetId) return;
  const sh = (window.shuttersRegistry || []).find(s => s.id === targetId);
  if (!sh) return;
  const val = Math.max(0, Math.min(100, Math.round(Number(pct))));
  sh.targetPercent = val;
  const inp = document.getElementById('shutter-range-input');
  if (inp && activeShutterId === sh.id) inp.value = Math.round(val);
  const ghPillFill = document.getElementById('gh-shutter-pill-fill');
  const popPct = document.getElementById('shutter-popup-pct');
  const popState = document.getElementById('shutter-popup-state');
  if (ghPillFill && activeShutterId === sh.id) ghPillFill.style.height = `${Math.round(val)}%`;
  if (popPct && activeShutterId === sh.id) popPct.textContent = `${Math.round(val)}%`;
  if (popState && activeShutterId === sh.id) {
    popState.textContent =
      val === 0 ? 'Komplett geöffnet (0%)' :
      val === 100 ? 'Komplett geschlossen (100%)' :
      `Zu ${val}% geschlossen`;
  }
  const fillBar = document.getElementById('shutter-slider-fill');
  const textBar = document.getElementById('shutter-slider-text');
  const readOut = document.getElementById('shutter-slider-readout');
  if (fillBar && activeShutterId === sh.id) fillBar.style.width = `${Math.round(val)}%`;
  if (textBar && activeShutterId === sh.id) textBar.textContent = Math.round(val) === 0 ? 'Offen · 0%' : (Math.round(val) === 100 ? 'Zu · 100%' : `${Math.round(val)}%`);
  if (readOut && activeShutterId === sh.id) readOut.textContent = `${Math.round(val)}%`;
};

function updateShuttersAndBadges() {
  if (!window.shuttersRegistry) return;
  const halfW = window.innerWidth / 2;
  const halfH = window.innerHeight / 2;

  window.shuttersRegistry.forEach(sh => {
    // Smooth electric motor animation towards targetPercent
    if (Math.abs(sh.currentPercent - sh.targetPercent) > 0.15) {
      sh.currentPercent += (sh.targetPercent - sh.currentPercent) * 0.12;
      updateSingleShutter3D(sh);
    } else if (sh.currentPercent !== sh.targetPercent) {
      sh.currentPercent = sh.targetPercent;
      updateSingleShutter3D(sh);
    }

    const badge = document.getElementById(`badge-${sh.id}`);
    if (!badge) return;

    // Rollladen-Button NUR anzeigen, wenn man sich direkt in diesem Raum befindet!
    const visibleInRoom = Boolean(activeRoomId && activeRoomId === sh.roomId);
    if (!visibleInRoom) {
      badge.style.display = 'none';
      return;
    }

    const projected = sh.worldPos.clone();
    projected.y *= groups[sh.apt].walls.scale.y;
    projected.project(camera);

    let sx = (projected.x * halfW) + halfW;
    let sy = -(projected.y * halfH) + halfH;
    // Falls man sich im Raum umgedreht hat (Fenster hinter der Kamera), Button am Bildschirmrand erreichbar halten
    if (projected.z > 1) {
      sx = window.innerWidth - sx;
      sy = window.innerHeight * 0.55;
    }
    const padX = Math.min(95, window.innerWidth * 0.22);
    sx = Math.max(padX, Math.min(window.innerWidth - padX, sx));
    sy = Math.max(78, Math.min(window.innerHeight - 95, sy));

    badge.style.display = 'inline-flex';
    badge.style.left = `${sx}px`;
    badge.style.top = `${sy}px`;
  });

  // Positioniere den schwebenden Licht-Button in Simons Zimmer (nur sichtbar wenn man in Simons Zimmer ist!)
  const lightBadge = document.getElementById('badge-light-simon');
  if (lightBadge && window.simonRoomLight) {
    if (activeRoomId !== 'label-simon') {
      lightBadge.style.display = 'none';
    } else {
      const lp = window.simonRoomLight.worldPos.clone();
      lp.y *= groups.w1.walls.scale.y;
      lp.project(camera);
      let lx = (lp.x * halfW) + halfW;
      let ly = -(lp.y * halfH) + halfH;
      const padX = Math.min(85, window.innerWidth * 0.2);
      lx = Math.max(padX, Math.min(window.innerWidth - padX, lx));
      ly = Math.max(82, Math.min(window.innerHeight - 95, ly));
      lightBadge.style.display = 'inline-flex';
      lightBadge.style.left = `${lx}px`;
      lightBadge.style.top = `${ly}px`;
    }
  }

  // Positioniere den schwebenden Tisch-LED-Button direkt am Schreibtisch in Simons Zimmer
  const ledBadge = document.getElementById('badge-led-simon');
  if (ledBadge && window.simonDeskLed) {
    if (activeRoomId !== 'label-simon') {
      ledBadge.style.display = 'none';
    } else {
      const dp = window.simonDeskLed.worldPos.clone();
      dp.y *= groups.w1.walls.scale.y;
      dp.project(camera);
      let dx = (dp.x * halfW) + halfW;
      let dy = -(dp.y * halfH) + halfH;
      const padX = Math.min(85, window.innerWidth * 0.2);
      dx = Math.max(padX, Math.min(window.innerWidth - padX, dx));
      dy = Math.max(82, Math.min(window.innerHeight - 95, dy));
      ledBadge.style.display = 'inline-flex';
      ledBadge.style.left = `${dx}px`;
      ledBadge.style.top = `${dy}px`;
    }
  }

  // Positioniere den schwebenden PC-Button direkt an Simons PC unter dem Schreibtisch
  const pcBadge = document.getElementById('badge-pc-simon');
  if (pcBadge && window.simonPc) {
    if (activeRoomId !== 'label-simon') {
      pcBadge.style.display = 'none';
    } else {
      const pp = window.simonPc.worldPos.clone();
      pp.y *= groups.w1.walls.scale.y;
      pp.project(camera);
      let pxScreen = (pp.x * halfW) + halfW;
      let pyScreen = -(pp.y * halfH) + halfH;
      const padX = Math.min(85, window.innerWidth * 0.2);
      pxScreen = Math.max(padX, Math.min(window.innerWidth - padX, pxScreen));
      pyScreen = Math.max(82, Math.min(window.innerHeight - 95, pyScreen));
      pcBadge.style.display = 'inline-flex';
      pcBadge.style.left = `${pxScreen}px`;
      pcBadge.style.top = `${pyScreen}px`;
    }
  }

  // Positioniere den schwebenden Licht-Button im Schlafzimmer (nur sichtbar wenn man im Schlafzimmer ist!)
  const schlafLightBadge = document.getElementById('badge-light-schlaf');
  if (schlafLightBadge && window.schlafRoomLight) {
    if (activeRoomId !== 'label-schlaf') {
      schlafLightBadge.style.display = 'none';
    } else {
      const slp = window.schlafRoomLight.worldPos.clone();
      slp.y *= groups.w2.walls.scale.y;
      slp.project(camera);
      let slx = (slp.x * halfW) + halfW;
      let sly = -(slp.y * halfH) + halfH;
      const padX = Math.min(85, window.innerWidth * 0.2);
      slx = Math.max(padX, Math.min(window.innerWidth - padX, slx));
      sly = Math.max(82, Math.min(window.innerHeight - 95, sly));
      schlafLightBadge.style.display = 'inline-flex';
      schlafLightBadge.style.left = `${slx}px`;
      schlafLightBadge.style.top = `${sly}px`;
    }
  }

  // Positioniere den schwebenden Decken-LED-Button oben an der Decke im Schlafzimmer
  const schlafLedBadge = document.getElementById('badge-led-schlaf');
  if (schlafLedBadge && window.schlafCeilingLed) {
    if (activeRoomId !== 'label-schlaf') {
      schlafLedBadge.style.display = 'none';
    } else {
      const clp = window.schlafCeilingLed.worldPos.clone();
      clp.y *= groups.w2.walls.scale.y;
      clp.project(camera);
      let clx = (clp.x * halfW) + halfW;
      let cly = -(clp.y * halfH) + halfH;
      const padX = Math.min(85, window.innerWidth * 0.2);
      clx = Math.max(padX, Math.min(window.innerWidth - padX, clx));
      cly = Math.max(82, Math.min(window.innerHeight - 95, cly));
      schlafLedBadge.style.display = 'inline-flex';
      schlafLedBadge.style.left = `${clx}px`;
      schlafLedBadge.style.top = `${cly}px`;
    }
  }

  // Positioniere den schwebenden Steckdosen-Button an der rechten Küchenwand neben dem Tisch
  const kuechePlugBadge = document.getElementById('badge-plug-kueche');
  if (kuechePlugBadge && window.kuecheSocket) {
    if (activeRoomId !== 'label-kueche') {
      kuechePlugBadge.style.display = 'none';
    } else {
      const kp = window.kuecheSocket.worldPos.clone();
      kp.y *= groups.w2.walls.scale.y;
      kp.project(camera);
      let kx = (kp.x * halfW) + halfW;
      let ky = -(kp.y * halfH) + halfH;
      const padX = Math.min(85, window.innerWidth * 0.2);
      kx = Math.max(padX, Math.min(window.innerWidth - padX, kx));
      ky = Math.max(82, Math.min(window.innerHeight - 95, ky));
      kuechePlugBadge.style.display = 'inline-flex';
      kuechePlugBadge.style.left = `${kx}px`;
      kuechePlugBadge.style.top = `${ky}px`;
    }
  }
}

initShuttersUI();
initGoogleHomeControls();

// ============================================================================
// RESIZE & RENDER LOOP
// ============================================================================
window.addEventListener('resize', () => {
  camera.aspect = window.innerWidth / window.innerHeight;
  camera.updateProjectionMatrix();
  renderer.setSize(window.innerWidth, window.innerHeight);
});

function animate(now) {
  requestAnimationFrame(animate);

  if (camAnim) {
    const t = Math.min(1, (now - camAnim.startTime) / camAnim.duration);
    const ease = 1 - Math.pow(1 - t, 3);
    camera.position.lerpVectors(camAnim.startPos, camAnim.endPos, ease);
    controls.target.lerpVectors(camAnim.startTarget, camAnim.endTarget, ease);
    if (camAnim.startFov !== undefined && camAnim.endFov !== undefined) {
      camera.fov = camAnim.startFov + (camAnim.endFov - camAnim.startFov) * ease;
      camera.updateProjectionMatrix();
    }
    if (t >= 1) camAnim = null;
  }

  ['w1', 'w2', 'common'].forEach(key => {
    const currentY = groups[key].walls.scale.y;
    if (Math.abs(currentY - targetWallScaleY) > 0.002) {
      groups[key].walls.scale.y += (targetWallScaleY - currentY) * 0.14;
    } else {
      groups[key].walls.scale.y = targetWallScaleY;
    }
  });

  controls.update();
  if (activeRoomId) {
    applyRoomIsolationAndCutaway(false);
  }
  updateFloatingLabels();
  updateShuttersAndBadges();
  renderer.render(scene, camera);
}

window.onload = function() {
  animate(performance.now());
};
