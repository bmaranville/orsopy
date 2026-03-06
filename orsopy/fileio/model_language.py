"""
Implementation of the simplified model language for the ORSO header.

It includes parsing of models from header or different input information and
resolving the model to a simple list of slabs.
"""

import warnings
from dataclasses import dataclass
from typing import Dict, List, Optional, Union

from lark import Lark, Transformer, v_args

from ..utils.chemical_formula import Formula
from . import model_complex
from .base import Header, Literal
from .model_building_blocks import (
    DENSITY_RESOLVERS, SPECIAL_MATERIALS, Composit, Layer, Material, 
    ModelParameters, SubStackType
)

# ==========================================
# 1. LARK GRAMMAR & TRANSFORMER
# ==========================================

orso_grammar = """
    ?start: stack
    
    stack: item ("|" item)*
    
    ?item: substack | layer
    
    # We use explicit helper rules to easily separate the outputs
    substack: [rep_count] "(" stack ")" [IN_KW environment]
    
    rep_count: SYMBOL
    environment: (SYMBOL | IN_KW)+
    layer: (SYMBOL | IN_KW)+
    
    IN_KW: "in"
    SYMBOL: /[^\s\|\(\)]+/

    %import common.WS
    %ignore WS
"""

class ModelTransformer(Transformer):
    def __init__(self, raw_text: str):
        super().__init__()
        self.raw_text = raw_text

    def stack(self, items):
        return items

    @v_args(meta=True)
    def layer(self, meta, args):
        # Slice the EXACT original string the user typed for this layer
        raw_string = self.raw_text[meta.start_pos : meta.end_pos].strip()
        
        # Exact legacy logic: split on the last space
        parts = raw_string.rsplit(None, 1)
        
        if len(parts) == 2:
            try:
                thickness = float(parts[1])
                material = parts[0]
            except ValueError:
                thickness = 0.0
                material = raw_string
        else:
            thickness = 0.0
            material = parts[0] if parts else ""
            
        return Layer(material=material, thickness=thickness)

    def rep_count(self, args):
        try:
            return int(args[0])
        except ValueError:
            return 1

    def environment(self, args):
        return " ".join(str(arg) for arg in args)

    @v_args(meta=True)
    def substack(self, meta, args):
        repetitions = 1
        sequence = None
        env = None

        # Dynamically assign arguments based on their evaluated types
        for arg in args:
            if isinstance(arg, int):
                repetitions = arg
            elif isinstance(arg, list):
                sequence = arg
            elif type(arg) is str: 
                # Strict 'type() is str' ignores Lark Tokens (like IN_KW) 
                # but catches the pure strings returned by the environment() method
                env = arg

        # 1. Get the exact raw string for this entire block
        full_str = self.raw_text[meta.start_pos : meta.end_pos]
        
        # 2. Slice out exactly what is inside the parentheses
        start_idx = full_str.find('(') + 1
        end_idx = full_str.rfind(')')
        original_stack_string = full_str[start_idx:end_idx].strip()

        return SubStack(
            repetitions=repetitions, 
            sequence=sequence, 
            stack=original_stack_string,
            environment=env
        )

# Instantiate the parser globally so it only compiles once
model_parser = Lark(orso_grammar, parser='lalr', propagate_positions=True)

# ==========================================
# 2. SHARED RESOLUTION LOGIC
# ==========================================

def _resolve_parsed_item(item: Union[Layer, SubStackType], resolvable_items: dict) -> Union[Layer, SubStackType]:
    """
    Takes a raw AST object from the Transformer and resolves its string 
    material name against dictionaries, formulas, and databases.
    """
    if isinstance(item, SubStackType):
        return item
    
    # If it's already a complex object (not a string), it doesn't need DB lookup
    if not isinstance(item.material, str):
        return item

    material_name = item.material
    thickness = item.thickness
    
    if material_name in resolvable_items:
        obj = resolvable_items[material_name]
        if isinstance(obj, SubStackType):
            # create a copy of the object to allow different environments for same key
            obj = obj.__class__.from_dict(obj.to_dict())
        if isinstance(obj, Material) or isinstance(obj, Composit):
            obj = Layer(material=obj, thickness=thickness)
        elif getattr(obj, "thickness", "ignore") is None:
            obj.thickness = thickness
    else:
        try:
            Formula(material_name, strict=True)
            obj = Layer(material=material_name, thickness=thickness)
            obj.original_name = material_name
        except ValueError:
            # try to resolve name directly with database
            res = None
            for resolver in DENSITY_RESOLVERS:
                res = resolver.resolve_item(material_name)
                if res is not None:
                    break
            if res is None:
                # assume name is a Formula to resolve within Layer
                obj = Layer(material=material_name, thickness=thickness)
                obj.original_name = material_name
            else:
                if "material" in res:
                    obj = Layer.from_dict(res)
                elif "composition" in res:
                    obj = Layer(material=Composit.from_dict(res), thickness=thickness)
                elif "formula" in res or "sld" in res:
                    obj = Layer(material=Material.from_dict(res), thickness=thickness)
                else:
                    obj = Layer(material=material_name, thickness=thickness)
                obj.original_name = material_name
                if getattr(obj, "thickness", "ignore") is None:
                    obj.thickness = thickness
                    
    return obj


# ==========================================
# 3. DATACLASSES
# ==========================================

@dataclass
class SubStack(Header, SubStackType):
    repetitions: int = 1
    stack: Optional[str] = None
    sequence: Optional[List[Layer]] = None
    sub_stack_class: Literal["SubStack"] = "SubStack"
    environment: Optional[Union[str, Material, Composit]] = None

    original_name = None

    def resolve_names(self, resolvable_items):
        if isinstance(self.environment, str):
            env_orig = self.environment
            if self.environment in resolvable_items:
                self.environment = resolvable_items[self.environment]
            elif self.environment in SPECIAL_MATERIALS:
                self.environment = SPECIAL_MATERIALS[self.environment]
            else:
                self.environment = Material(formula=self.environment)
            self.environment.original_name = env_orig
        
        if self.environment is not None:
            resolvable_items = {"environment": self.environment, **resolvable_items}

        if self.stack is None and self.sequence is None:
            raise ValueError("SubStack has to either define stack or sequence")
            
        if self.sequence is None:
            # Parse the string with Lark
            tree = model_parser.parse(self.stack)
            # Instantiate locally with raw text
            self.sequence = ModelTransformer(self.stack).transform(tree)

        # --- NEW FIX: Unified Loop ---
        # Iterate over the sequence (whether user-provided or freshly parsed)
        output = []
        for li in self.sequence:
            # 1. Resolve through the DB and dict lookups
            resolved_li = _resolve_parsed_item(li, resolvable_items)
            
            # 2. Call native layer/substack resolution recursively
            if hasattr(resolved_li, "resolve_names"):
                resolved_li.resolve_names(resolvable_items)
                
            output.append(resolved_li)
            
        self.sequence = output

    def resolve_defaults(self, defaults: ModelParameters):
        for li in self.sequence:
            if hasattr(li, "resolve_defaults"):
                li.resolve_defaults(defaults)
        if self.environment is not None:
            self.environment.resolve_defaults(defaults)

    def resolve_to_blocks(self) -> List[Union[Layer, SubStackType]]:
        blocks = list(self.sequence)
        added = 0
        for i in range(len(blocks)):
            if isinstance(blocks[i + added], Layer):
                if blocks[i + added].material is None:
                    blocks[i + added].generate_material()
                blocks[i + added].material.generate_density()
            else:
                obj = blocks.pop(i + added)
                sub_blocks = obj.resolve_to_blocks()
                blocks = blocks[: i + added] + sub_blocks + blocks[i + added :]
                added += len(sub_blocks) - 1
        return blocks

    def resolve_to_layers(self) -> List[Layer]:
        layers = list(self.sequence)
        added = 0
        for i in range(len(layers)):
            if isinstance(layers[i + added], Layer):
                if layers[i + added].material is None:
                    layers[i + added].generate_material()
                layers[i + added].material.generate_density()
            else:
                obj = layers.pop(i + added)
                sub_layers = obj.resolve_to_layers()
                layers = layers[: i + added] + sub_layers + layers[i + added :]
                added += len(sub_layers) - 1
        return layers * self.repetitions


SUBSTACK_TYPE = SubStack
for T in SubStackType.__subclasses__():
    SUBSTACK_TYPE = Union[SUBSTACK_TYPE, T]


@dataclass
class ItemChanger(Header):
    """
    Allows to define a simple change in SubStackType item by
    just updating a selected set of parameters.
    """
    like: str
    but: dict
    original_name = None


@dataclass
class SampleModel(Header):
    stack: str
    origin: Optional[str] = None
    sub_stacks: Optional[Dict[str, Union[ItemChanger, SUBSTACK_TYPE]]] = None
    layers: Optional[Dict[str, Layer]] = None
    materials: Optional[Dict[str, Material]] = None
    composits: Optional[Dict[str, Composit]] = None
    globals: Optional[ModelParameters] = None
    reference: Optional[str] = None

    def __post_init__(self):
        super().__post_init__()
        names = []
        for di in [self.sub_stacks, self.layers, self.materials, self.composits]:
            if di is None:
                continue
            for ni in di.keys():
                if ni in names:
                    warnings.warn(f'Duplicate name "{ni}" in SampleModel definition')
                names.append(ni)

    @property
    def resolvable_items(self):
        output = {}
        if self.sub_stacks:
            for key, ssi in self.sub_stacks.items():
                if isinstance(ssi, ItemChanger):
                    ssi_ref = self.sub_stacks[ssi.like]
                    ssi_ref_data = ssi_ref.to_dict()
                    ssi_ref_data.update(ssi.but)
                    ssi = ssi_ref.__class__.from_dict(ssi_ref_data)
                    self.sub_stacks[key] = ssi
                ssi.original_name = key
            output.update(self.sub_stacks)
        if self.layers:
            for key, li in self.layers.items():
                li.original_name = key
            output.update(self.layers)
        if self.materials:
            for key, mi in self.materials.items():
                mi.original_name = key
            output.update(self.materials)
        if self.composits:
            for key, ci in self.composits.items():
                ci.original_name = key
            output.update(self.composits)
        return output

    def resolve_stack(self):
        defaults = self.globals if self.globals is not None else ModelParameters()
        ri = self.resolvable_items
        
        # Parse the root stack with Lark
        tree = model_parser.parse(self.stack)
        # Instantiate the transformer with the raw string
        raw_sequence = ModelTransformer(self.stack).transform(tree)
        
        output = []
        for raw_item in raw_sequence:
            resolved_obj = _resolve_parsed_item(raw_item, ri)

            if hasattr(resolved_obj, "resolve_names"):
                resolved_obj.resolve_names(ri)

            if hasattr(resolved_obj, "resolve_defaults"):
                resolved_obj.resolve_defaults(defaults)
            output.append(resolved_obj)
            
        return output

    def resolve_to_blocks(self) -> List[Union[Layer, SubStackType]]:
        blocks = self.resolve_stack()
        added = 0
        for i in range(len(blocks)):
            if isinstance(blocks[i + added], Layer):
                if blocks[i + added].material is None:
                    blocks[i + added].generate_material()
                blocks[i + added].material.generate_density()
            else:
                obj = blocks.pop(i + added)
                sub_blocks = obj.resolve_to_blocks()
                blocks = blocks[: i + added] + sub_blocks + blocks[i + added :]
                added += len(sub_blocks) - 1
        return blocks

    def resolve_to_layers(self) -> List[Layer]:
        layers = self.resolve_stack()
        added = 0
        for i in range(len(layers)):
            if isinstance(layers[i + added], Layer):
                if layers[i + added].material is None:
                    layers[i + added].generate_material()
                layers[i + added].material.generate_density()
            else:
                obj = layers.pop(i + added)
                sub_layers = obj.resolve_to_layers()
                layers = layers[: i + added] + sub_layers + layers[i + added :]
                added += len(sub_layers) - 1
        return layers